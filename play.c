#define _GNU_SOURCE
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/XShm.h>
#include <X11/extensions/XTest.h>
#include <X11/extensions/shape.h>
#include <X11/extensions/Xfixes.h>
#include <png.h>
#include <stdio.h>
#include <jpeglib.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/shm.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define MAX_DEVICES 8
#define MAX_TEMPLATES 64
#define MAX_PATH_LEN 1024
#define BIG_W 480
#define BIG_H 270

typedef struct { int w, h; unsigned char *p; } Image;
typedef struct { char path[MAX_PATH_LEN]; Image image; Image small; Image half; } Template;
typedef struct { int type, code, value; } Event;
typedef struct { double at; int device, order, count; Event events[32]; } Frame;
typedef struct {
    char name[256];
    int fd;
    unsigned char types[EV_MAX + 1];
    unsigned char codes[EV_MAX + 1][KEY_MAX + 1];
    int id[4];
} Device;

static Display *display_main;
static Window overlay_win;
static GC overlay_gc;
static int screen_w, screen_h;
static XImage *capture_image;
static XShmSegmentInfo capture_shm;
static unsigned char *screen_gray;
static Image screen_big;
static Image screen_half;
static int capture_w, capture_h;
static Template p1_templates[MAX_TEMPLATES], p2_templates[MAX_TEMPLATES];
static Template retry_t, skip_t, heal_t, completed_t, failed_t;
static int p1_count, p2_count;
static Device devices[MAX_DEVICES];
static int device_count;
static Frame *frames;
static size_t frame_count, frame_cap;
static atomic_int quitting, retry_event, skip_event;
static atomic_int retry_visible, skip_visible;
static pthread_t watcher_thread;
static int watcher_running;
static int click_fd = -1;
static int retry_x = 1154, retry_y = 847, skip_x = 1802, skip_y = 1028;
static double threshold = 0.80;
static int one_shot;
static int loops_done;
static char overlay_status[96] = "READY";
static char overlay_log[96] = "";
static char mission_result[16] = "UNKNOWN";
static pthread_mutex_t overlay_lock = PTHREAD_MUTEX_INITIALIZER;

static double now_sec(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static void set_overlay(const char *status, const char *log_line) {
    pthread_mutex_lock(&overlay_lock);
    if (status) snprintf(overlay_status, sizeof(overlay_status), "%s", status);
    if (log_line) snprintf(overlay_log, sizeof(overlay_log), "%s", log_line);
    pthread_mutex_unlock(&overlay_lock);
}

static void overlay_draw(void) {
    if (!display_main || !overlay_win) return;
    char a[96], b[96];
    pthread_mutex_lock(&overlay_lock);
    snprintf(a, sizeof(a), "%s", overlay_status);
    snprintf(b, sizeof(b), "%s", overlay_log);
    pthread_mutex_unlock(&overlay_lock);
    XClearWindow(display_main, overlay_win);
    XSetForeground(display_main, overlay_gc, 0xFFFFFF);
    XDrawString(display_main, overlay_win, overlay_gc, 8, 19, a, (int)strlen(a));
    XSetForeground(display_main, overlay_gc, 0xFFD166);
    XDrawString(display_main, overlay_win, overlay_gc, 8, 40, b, (int)strlen(b));
    XFlush(display_main);
}

static void overlay_init(void) {
    int x = screen_w - 285;
    if (x < 0) x = 0;
    overlay_win = XCreateSimpleWindow(display_main, DefaultRootWindow(display_main), x, 20, 275, 50,
                                       1, 0x4A90E2, 0x101820);
    XSetWindowAttributes attr;
    attr.override_redirect = True;
    XChangeWindowAttributes(display_main, overlay_win, CWOverrideRedirect, &attr);
    XStoreName(display_main, overlay_win, "AOTR C Player");
    overlay_gc = XCreateGC(display_main, overlay_win, 0, NULL);
    XMapRaised(display_main, overlay_win);
    XFixesSetWindowShapeRegion(display_main, overlay_win, ShapeInput, 0, 0, None);
    overlay_draw();
}

static void gray_from_pixel(unsigned long pixel, Visual *visual, unsigned char *out) {
    unsigned long rm = visual->red_mask, gm = visual->green_mask, bm = visual->blue_mask;
    int rs = __builtin_ctzl(rm), gs = __builtin_ctzl(gm), bs = __builtin_ctzl(bm);
    unsigned long rv = (pixel & rm) >> rs, gv = (pixel & gm) >> gs, bv = (pixel & bm) >> bs;
    unsigned long rmax = rm >> rs, gmax = gm >> gs, bmax = bm >> bs;
    unsigned int r = (unsigned int)(rv * 255 / (rmax ? rmax : 1));
    unsigned int g = (unsigned int)(gv * 255 / (gmax ? gmax : 1));
    unsigned int b = (unsigned int)(bv * 255 / (bmax ? bmax : 1));
    *out = (unsigned char)((77 * r + 150 * g + 29 * b) >> 8);
}

static void resize_area(const Image *src, Image *dst, int dw, int dh) {
    if (!dst->p || dst->w != dw || dst->h != dh) {
        free(dst->p); dst->p = malloc((size_t)dw * dh); dst->w = dw; dst->h = dh;
    }
    if (!dst->p || !src->p) return;
    for (int y = 0; y < dh; y++) {
        int y0 = (int)((int64_t)y * src->h / dh);
        int y1 = (int)((int64_t)(y + 1) * src->h / dh);
        if (y1 <= y0) y1 = y0 + 1;
        for (int x = 0; x < dw; x++) {
            int x0 = (int)((int64_t)x * src->w / dw);
            int x1 = (int)((int64_t)(x + 1) * src->w / dw);
            if (x1 <= x0) x1 = x0 + 1;
            unsigned int sum = 0, n = 0;
            for (int sy = y0; sy < y1 && sy < src->h; sy++)
                for (int sx = x0; sx < x1 && sx < src->w; sx++) {
                    sum += src->p[(size_t)sy * src->w + sx]; n++;
                }
            dst->p[(size_t)y * dw + x] = (unsigned char)(sum / (n ? n : 1));
        }
    }
}

static int load_png(const char *path, Image *out) {
    png_image im; memset(&im, 0, sizeof(im)); im.version = PNG_IMAGE_VERSION;
    if (!png_image_begin_read_from_file(&im, path)) return -1;
    im.format = PNG_FORMAT_GRAY;
    out->w = (int)im.width; out->h = (int)im.height;
    out->p = malloc(PNG_IMAGE_SIZE(im));
    if (!out->p || !png_image_finish_read(&im, NULL, out->p, 0, NULL)) {
        free(out->p); out->p = NULL; png_image_free(&im); return -1;
    }
    png_image_free(&im); return 0;
}

static int load_jpeg(const char *path, Image *out) {
    FILE *f = fopen(path, "rb"); if (!f) return -1;
    struct jpeg_decompress_struct c; struct jpeg_error_mgr e;
    c.err = jpeg_std_error(&e); jpeg_create_decompress(&c); jpeg_stdio_src(&c, f);
    if (jpeg_read_header(&c, TRUE) != JPEG_HEADER_OK) { jpeg_destroy_decompress(&c); fclose(f); return -1; }
    c.out_color_space = JCS_RGB; jpeg_start_decompress(&c);
    out->w = (int)c.output_width; out->h = (int)c.output_height;
    out->p = malloc((size_t)out->w * out->h);
    if (!out->p) { jpeg_destroy_decompress(&c); fclose(f); return -1; }
    unsigned char *row = malloc((size_t)out->w * 3);
    if (!row) { free(out->p); out->p=NULL; jpeg_destroy_decompress(&c); fclose(f); return -1; }
    while (c.output_scanline < c.output_height) {
        JSAMPROW rows[1] = { row }; int y = (int)c.output_scanline;
        jpeg_read_scanlines(&c, rows, 1);
        for (int x = 0; x < out->w; x++)
            out->p[(size_t)y * out->w + x] = (unsigned char)((77*row[x*3] + 150*row[x*3+1] + 29*row[x*3+2]) >> 8);
    }
    free(row); jpeg_finish_decompress(&c); jpeg_destroy_decompress(&c); fclose(f); return 0;
}

static int load_image(const char *path, Image *out) {
    memset(out, 0, sizeof(*out));
    size_t n = strlen(path);
    if (n >= 4 && (!strcasecmp(path+n-4, ".png"))) return load_png(path, out);
    if (n >= 5 && (!strcasecmp(path+n-5, ".jpeg"))) return load_jpeg(path, out);
    if (n >= 4 && (!strcasecmp(path+n-4, ".jpg"))) return load_jpeg(path, out);
    return -1;
}

static int load_template(Template *t, const char *path) {
    memset(t, 0, sizeof(*t));
    if (load_image(path, &t->image) != 0) return -1;
    snprintf(t->path, sizeof(t->path), "%s", path);
    resize_area(&t->image, &t->small, BIG_W, BIG_H);
    resize_area(&t->image, &t->half, (t->image.w+1)/2, (t->image.h+1)/2);
    return 0;
}

static int load_dir(Template *list, int *count, const char *dirpath) {
    DIR *d = opendir(dirpath); if (!d) return 0;
    struct dirent *ent;
    while ((ent = readdir(d)) && *count < MAX_TEMPLATES) {
        if (ent->d_name[0] == '.') continue;
        char path[MAX_PATH_LEN]; snprintf(path, sizeof(path), "%s/%s", dirpath, ent->d_name);
        struct stat st; if (stat(path, &st) || !S_ISREG(st.st_mode)) continue;
        Template t;
        if (load_template(&t, path) == 0) list[(*count)++] = t;
    }
    closedir(d); return *count;
}

static double corr_at(const unsigned char *a, int aw, const unsigned char *b, int bw,
                      int w, int h, int bx, int by, int sample_step) {
    double sa=0, sb=0, saa=0, sbb=0, sab=0; size_t n=0;
    for (int y=0; y<h; y+=sample_step) for (int x=0; x<w; x+=sample_step) {
        double av=a[(size_t)y*aw+x], bv=b[(size_t)(by+y)*bw+bx+x];
        sa+=av; sb+=bv; saa+=av*av; sbb+=bv*bv; sab+=av*bv; n++;
    }
    if (!n) return -1.0;
    double va=saa-sa*sa/n, vb=sbb-sb*sb/n, cov=sab-sa*sb/n;
    if (va < 1e-9 || vb < 1e-9) return -1.0;
    return (double)(cov/sqrt((double)(va*vb)));
}

static double match_large(const Template *t, const Image *frame) {
    Image cur={0}; resize_area(frame, &cur, BIG_W, BIG_H);
    double scene=corr_at(t->small.p, BIG_W, cur.p, BIG_W, BIG_W, BIG_H, 0, 0, 1);
    int x=BIG_W*40/100, w=BIG_W*44/100, h=BIG_H/4;
    double hud=corr_at(t->small.p+(size_t)x, BIG_W, cur.p+(size_t)x, BIG_W, w, h, 0, 0, 1);
    free(cur.p); return fmax(scene, hud);
}

/* Scan a bounded rectangle on the half-size screen. The coarse pass samples
 * every second template pixel; a one-pixel pass refines the best location. */
static double match_region(const Template *t, const Image *half, int x0, int y0,
                           int x1, int y1, int *bestx, int *besty, int coarse, int sample_step) {
    const Image *th=&t->half;
    if (!th->p || th->w>half->w || th->h>half->h) return -1.0;
    x0/=2; y0/=2; x1/=2; y1/=2;
    if (x0<0) x0=0;
    if(y0<0)y0=0;
    if(x1>half->w-th->w)x1=half->w-th->w;
    if(y1>half->h-th->h)y1=half->h-th->h;
    double best=-1.0; int bx=x0, by=y0;
    int grid=coarse>0?coarse:1;
    for(int y=y0;y<=y1;y+=grid) for(int x=x0;x<=x1;x+=grid) {
        double s=corr_at(th->p,th->w,half->p,half->w,th->w,th->h,x,y,sample_step);
        if(s>best){best=s;bx=x;by=y;}
    }
    int rx0=bx-grid, ry0=by-grid, rx1=bx+grid, ry1=by+grid;
    if(rx0<x0)rx0=x0;
    if(ry0<y0)ry0=y0;
    if(rx1>x1)rx1=x1;
    if(ry1>y1)ry1=y1;
    for(int y=ry0;y<=ry1;y++) for(int x=rx0;x<=rx1;x++) {
        double s=corr_at(th->p,th->w,half->p,half->w,th->w,th->h,x,y,1);
        if(s>best){best=s;bx=x;by=y;}
    }
    if(bestx)*bestx=bx*2;
    if(besty)*besty=by*2;
    return best;
}

static int capture_gray(Image *out) {
    if (!capture_image || !XShmGetImage(display_main, DefaultRootWindow(display_main), capture_image, 0, 0, AllPlanes)) return -1;
    Visual *v=DefaultVisual(display_main,DefaultScreen(display_main));
    int bpp=(capture_image->bits_per_pixel+7)/8;
    for(int y=0;y<capture_h;y++){unsigned char *row=(unsigned char*)capture_image->data+(size_t)y*capture_image->bytes_per_line;for(int x=0;x<capture_w;x++){unsigned long px=0;memcpy(&px,row+(size_t)x*bpp,bpp);gray_from_pixel(px,v,&screen_gray[(size_t)y*capture_w+x]);}}
    out->w=capture_w;out->h=capture_h;out->p=screen_gray;
    resize_area(out,&screen_big,BIG_W,BIG_H);
    resize_area(out,&screen_half,capture_w/2,capture_h/2);
    return 0;
}

static double find_trigger(Template *templates, int count, const Image *screen) {
    double best=-1;
    for(int i=0;i<count;i++) {
        double s;
        if(templates[i].image.w>400 && templates[i].image.h>300) s=match_large(&templates[i],screen);
        else s=match_region(&templates[i],&screen_half,0,0,capture_w,capture_h,NULL,NULL,4,3);
        if(s>best)best=s;
    }
    return best;
}

static void emit_event(int fd, unsigned short type, unsigned short code, int value) {
    struct input_event ev;memset(&ev,0,sizeof(ev));ev.type=type;ev.code=code;ev.value=value;
    ssize_t wrote=write(fd,&ev,sizeof(ev));
    if(wrote!=(ssize_t)sizeof(ev)) atomic_store(&quitting,1);
}

static int click_at(Display *dpy, int x, int y) {
    if (!dpy) return -1;
    XTestFakeMotionEvent(dpy, DefaultScreen(dpy), x, y, CurrentTime);
    if(click_fd>=0){emit_event(click_fd,EV_KEY,BTN_LEFT,1);emit_event(click_fd,EV_SYN,SYN_REPORT,0);}
    else XTestFakeButtonEvent(dpy, 1, True, CurrentTime);
    XFlush(dpy);
    usleep(20000);
    if(click_fd>=0){emit_event(click_fd,EV_KEY,BTN_LEFT,0);emit_event(click_fd,EV_SYN,SYN_REPORT,0);}
    else XTestFakeButtonEvent(dpy, 1, False, CurrentTime);
    XFlush(dpy);
    return 0;
}

static int region_score(const Template *t, const Image *half, int cx, int cy, int radius, int *x, int *y) {
    return (int)(match_region(t,half,cx-radius,cy-radius,cx+radius,cy+radius,x,y,2,1)*10000);
}

static int match_button(const Template *t, const Image *half, int cx, int cy, int radius, double *score) {
    int x,y; int s=region_score(t,half,cx,cy,radius,&x,&y); *score=s/10000.0; return *score>=threshold;
}

static void capture_mission_result(const Image *half) {
    double a=-1,b=-1;
    int cx=capture_w/2,cy=capture_h/2;
    if(completed_t.image.p) a=match_region(&completed_t,half,cx-capture_w/3,cy-capture_h/3,cx+capture_w/3,cy+capture_h/3,NULL,NULL,4,2);
    if(failed_t.image.p) b=match_region(&failed_t,half,cx-capture_w/3,cy-capture_h/3,cx+capture_w/3,cy+capture_h/3,NULL,NULL,4,2);
    const char *result = a>=threshold ? "SUCCESS" : b>=threshold ? "FAIL" : NULL;
    if(result){char log[96];snprintf(log,sizeof(log),"MISSION %s",result);pthread_mutex_lock(&overlay_lock);snprintf(mission_result,sizeof(mission_result),"%s",result);pthread_mutex_unlock(&overlay_lock);set_overlay(NULL,log);}
}

static void *watch_buttons(void *unused) {
    (void)unused;
    Display *dpy=XOpenDisplay(NULL); if(!dpy)return NULL;
    int svw=DisplayWidth(dpy,DefaultScreen(dpy)), svh=DisplayHeight(dpy,DefaultScreen(dpy));
    XImage *img=NULL; XShmSegmentInfo sh; memset(&sh,0,sizeof(sh));
    img=XShmCreateImage(dpy,DefaultVisual(dpy,DefaultScreen(dpy)),DefaultDepth(dpy,DefaultScreen(dpy)),ZPixmap,NULL,&sh,svw,svh);
    if(!img){XCloseDisplay(dpy);return NULL;}
    sh.shmid=shmget(IPC_PRIVATE,(size_t)img->bytes_per_line*svh,IPC_CREAT|0600);
    if(sh.shmid<0){XDestroyImage(img);XCloseDisplay(dpy);return NULL;}
    sh.shmaddr=img->data=shmat(sh.shmid,NULL,0); sh.readOnly=False;
    XShmAttach(dpy,&sh); XSync(dpy,False); shmctl(sh.shmid,IPC_RMID,0);
    unsigned char *g=malloc((size_t)svw*svh); Image full={svw,svh,g}, half={0};
    Image big={0}; double next_skip=0,next_retry=0,next_heal=0,next_mission=0;
    double retry_since=0; int retry_y_current=retry_y; int heal_active=0;
    while(!atomic_load(&quitting)) {
        double now=now_sec();
        if(XShmGetImage(dpy,DefaultRootWindow(dpy),img,0,0,AllPlanes)) {
            Visual *v=DefaultVisual(dpy,DefaultScreen(dpy));
            int bpp=(img->bits_per_pixel+7)/8;
            for(int y=0;y<svh;y++){unsigned char *row=(unsigned char*)img->data+(size_t)y*img->bytes_per_line;for(int x=0;x<svw;x++){unsigned long px=0;memcpy(&px,row+(size_t)x*bpp,bpp);gray_from_pixel(px,v,&g[(size_t)y*svw+x]);}}
            resize_area(&full,&half,svw/2,svh/2); resize_area(&full,&big,BIG_W,BIG_H);
            if(now>=next_mission){capture_mission_result(&half);next_mission=now+0.25;}
            double hs=-1; int heal=0,heal_checked=0;
            if(heal_t.image.p && now>=next_heal){heal_checked=1;next_heal=now+0.25;heal=match_button(&heal_t,&half,svw/2,svh/2,svw/2,&hs);}
            if(heal) {
                if(!heal_active){set_overlay("HEAL", "HEAL DETECTED");printf("\n[AUTO] HEAL detected (%.3f); prioritizing heal actions.\n",hs);fflush(stdout);}
                heal_active=1;
                const int xy[3][2]={{668,890},{1193,280},{1863,73}};
                for(int i=0;i<3&&!atomic_load(&quitting);i++) for(int k=0;k<2;k++) {click_at(dpy,xy[i][0],xy[i][1]); if(k==0)usleep(500000);}
                next_heal=now_sec()+0.25;
            }
            double ss=-1,rs=-1;
            int skip=skip_t.image.p && match_button(&skip_t,&half,skip_x,skip_y,100,&ss);
            int retry=retry_t.image.p && match_button(&retry_t,&half,retry_x,retry_y,110,&rs);
            if(skip){
                if(!atomic_exchange(&skip_visible,1)){atomic_store(&skip_event,1);set_overlay(NULL,"SKIP DETECTED");printf("\n[AUTO] SKIP detected (%.3f); clicking (%d,%d).\n",ss,skip_x,skip_y);fflush(stdout);}
                if(now>=next_skip){click_at(dpy,skip_x,skip_y);next_skip=now_sec()+0.1;}
            } else {atomic_store(&skip_visible,0);atomic_store(&skip_event,0);}
            if(retry){
                if(!atomic_exchange(&retry_visible,1)){atomic_store(&retry_event,1);retry_since=now;retry_y_current=retry_y;set_overlay(NULL,"RETRY DETECTED");printf("\n[AUTO] RETRY detected (%.3f); clicking (%d,%d).\n",rs,retry_x,retry_y_current);fflush(stdout);}
                if(now>=next_retry){
                    if(now-retry_since>=5.0)retry_y_current=retry_y-(int)((now-retry_since-5.0)/0.1)-1;
                    click_at(dpy,retry_x,retry_y_current);next_retry=now_sec()+0.1;
                }
            } else {atomic_store(&retry_visible,0);retry_since=0;retry_y_current=retry_y;}
        if(heal_checked&&!heal)heal_active=0;
        }
        usleep(20000);
    }
    free(half.p);free(big.p);free(g);XShmDetach(dpy,&sh);XDestroyImage(img);shmdt(sh.shmaddr);XCloseDisplay(dpy);
    return NULL;
}

static int parse_number_after(const char *line, const char *needle, int *value) {
    const char *p=strstr(line,needle); if(!p)return 0; p+=strlen(needle); while(*p==' ')p++; *value=atoi(p); return 1;
}

static int push_frame(Frame *f) {
    if(frame_count==frame_cap){size_t n=frame_cap?frame_cap*2:4096;Frame *p=realloc(frames,n*sizeof(*p));if(!p)return -1;frames=p;frame_cap=n;}
    frames[frame_count++]=*f; return 0;
}

static int parse_recording(const char *path) {
    device_count=0;
    FILE *fp=fopen(path,"r"); if(!fp){perror(path);return -1;}
    char *line=NULL;size_t cap=0;ssize_t len;int curdev=-1,curtype=-1,inside_frame=0,frame_has=0,order=0;
    Frame fr;memset(&fr,0,sizeof(fr));
    while((len=getline(&line,&cap,fp))!=-1){
        if(strncmp(line,"- node:",7)==0){if(inside_frame&&frame_has&&push_frame(&fr))goto fail;curdev=device_count;if(device_count>=MAX_DEVICES){fprintf(stderr,"Too many devices in recording\n");goto fail;}Device*d=&devices[device_count++];memset(d,0,sizeof(*d));d->fd=-1;d->id[0]=BUS_USB;snprintf(d->name,sizeof(d->name),"input-%d",curdev);inside_frame=0;frame_has=0;continue;}
        if(curdev<0)continue;
        Device*d=&devices[curdev];
        char *name=strstr(line,"# Name:");if(name){name+=7;while(*name==' ')name++;name[strcspn(name,"\r\n")]=0;snprintf(d->name,sizeof(d->name),"%s",name);}
        int v;
        if(parse_number_after(line,"Event type ",&v)){curtype=v;if(v>=0&&v<=EV_MAX&&v!=EV_SYN&&v!=EV_REP)d->types[v]=1;}
        if(parse_number_after(line,"Event code ",&v)&&curtype>=0&&curtype<=EV_MAX&&v>=0&&v<=KEY_MAX&&curtype!=EV_SYN&&curtype!=EV_REP)d->codes[curtype][v]=1;
        unsigned int bus,vendor,product,version;
        if(sscanf(line,"    # ID: bus 0x%x vendor 0x%x product 0x%x version 0x%x",&bus,&vendor,&product,&version)==4){d->id[0]=bus;d->id[1]=vendor;d->id[2]=product;d->id[3]=version;}
        if(strstr(line,"- evdev:")){if(inside_frame&&frame_has&&push_frame(&fr))goto fail;memset(&fr,0,sizeof(fr));fr.device=curdev;fr.order=order++;inside_frame=1;frame_has=0;continue;}
        if(inside_frame){int sec,usec,type,code,value;
            if(sscanf(line," %*[-] [%d, %d, %d, %d, %d",&sec,&usec,&type,&code,&value)==5){
                if(!frame_has){fr.at=(double)sec+(double)usec/1e6;frame_has=1;}
                if(fr.count<32){fr.events[fr.count++]=(Event){type,code,value};}
            }
        }
    }
    if(inside_frame&&frame_has&&push_frame(&fr))goto fail;
    free(line);fclose(fp);return frame_count?0:-1;
fail: free(line);fclose(fp);return -1;
}

static int frame_cmp(const void *aa,const void *bb){const Frame*a=aa,*b=bb;if(a->at<b->at)return -1;if(a->at>b->at)return 1;return a->order-b->order;}

static int create_uinput(Device *d) {
    int fd=open("/dev/uinput",O_WRONLY|O_NONBLOCK);if(fd<0)return -1;
    for(int t=0;t<=EV_MAX;t++)if(d->types[t]&&t!=EV_REP){if(ioctl(fd,UI_SET_EVBIT,t)<0)goto bad;
        for(int c=0;c<=KEY_MAX;c++)if(d->codes[t][c]){
            unsigned long request=0;
            switch(t){case EV_KEY:request=UI_SET_KEYBIT;break;case EV_REL:request=UI_SET_RELBIT;break;case EV_ABS:request=UI_SET_ABSBIT;break;case EV_MSC:request=UI_SET_MSCBIT;break;case EV_SW:request=UI_SET_SWBIT;break;case EV_LED:request=UI_SET_LEDBIT;break;case EV_SND:request=UI_SET_SNDBIT;break;case EV_FF:request=UI_SET_FFBIT;break;default:break;}
            if(request&&ioctl(fd,request,c)<0)goto bad;
        }}
    struct uinput_setup setup;memset(&setup,0,sizeof(setup));setup.id.bustype=d->id[0];setup.id.vendor=d->id[1];setup.id.product=d->id[2];setup.id.version=d->id[3];snprintf(setup.name,sizeof(setup.name),"AOTR C replay %.60s",d->name);
    if(ioctl(fd,UI_DEV_SETUP,&setup)<0||ioctl(fd,UI_DEV_CREATE)<0)goto bad;
    d->fd=fd;usleep(100000);return 0;
bad: {int e=errno;close(fd);errno=e;return -1;}
}

static int create_click_device(void) {
    int fd=open("/dev/uinput",O_WRONLY|O_NONBLOCK);if(fd<0)return -1;
    if(ioctl(fd,UI_SET_EVBIT,EV_KEY)<0||ioctl(fd,UI_SET_KEYBIT,BTN_LEFT)<0){close(fd);return -1;}
    struct uinput_setup setup;memset(&setup,0,sizeof(setup));setup.id.bustype=BUS_USB;setup.id.vendor=0xA07;setup.id.product=2;setup.id.version=1;
    snprintf(setup.name,sizeof(setup.name),"AOTR C click");
    if(ioctl(fd,UI_DEV_SETUP,&setup)<0||ioctl(fd,UI_DEV_CREATE)<0){close(fd);return -1;}
    usleep(100000);return fd;
}

static void release_devices(void){for(int d=0;d<device_count;d++)if(devices[d].fd>=0){for(int c=0;c<=KEY_MAX;c++)if(devices[d].codes[EV_KEY][c])emit_event(devices[d].fd,EV_KEY,(unsigned short)c,0);emit_event(devices[d].fd,EV_SYN,SYN_REPORT,0);ioctl(devices[d].fd,UI_DEV_DESTROY);close(devices[d].fd);devices[d].fd=-1;}}

static double macro_duration(void){double d=0;for(size_t i=0;i<frame_count;i++)if(frames[i].at>d)d=frames[i].at;return d;}

static int poll_escape(void){char keys[32];XQueryKeymap(display_main,keys);KeyCode code=XKeysymToKeycode(display_main,XK_Escape);return (keys[code>>3]&(1<<(code&7)))!=0;}

static double replay_macro(const char *name, int phase) {
    if(parse_recording(name)){fprintf(stderr,"Could not load input recording: %s\n",name);atomic_store(&quitting,1);return 0;}
    qsort(frames,frame_count,sizeof(*frames),frame_cmp);
    double duration=macro_duration();
    for(int d=0;d<device_count;d++)if(create_uinput(&devices[d])<0){fprintf(stderr,"Cannot create uinput device for %s: %s. Check /dev/uinput permissions.\n",devices[d].name,strerror(errno));release_devices();atomic_store(&quitting,1);return 0;}
    char status[96];snprintf(status,sizeof(status),"PLAY %s",phase==1?"1":"2");set_overlay(status,NULL);overlay_draw();
    printf("\n[+] Replaying '%s' (%zu native input frames, %.2f sec recorded; C uinput scheduler)...\n",name,frame_count,duration);fflush(stdout);
    double start=now_sec(),next_draw=start;int stopped=0;
    for(size_t i=0;i<frame_count&&!atomic_load(&quitting);i++){
        double target=start+frames[i].at;
        while(!atomic_load(&quitting)){
            double now=now_sec();
            if(poll_escape()){atomic_store(&quitting,1);break;}
            if(now>=next_draw){next_draw=now+0.1;overlay_draw();}
            if(atomic_load(&retry_event)){stopped=1;break;}
            if(phase==1&&atomic_load(&skip_event)){stopped=1;break;}
            if(now>=target)break;
            double rem=target-now;if(rem>0.002){struct timespec ts={0,(long)((rem-0.001)*1e9)};nanosleep(&ts,NULL);}
        }
        if(stopped||atomic_load(&quitting))break;
        Frame *f=&frames[i];Device*d=&devices[f->device];
        for(int j=0;j<f->count;j++){
            Event *e=&f->events[j];if(e->type==EV_SYN)continue;
            struct input_event ev;memset(&ev,0,sizeof(ev));ev.type=(unsigned short)e->type;ev.code=(unsigned short)e->code;ev.value=e->value;
            if(write(d->fd,&ev,sizeof(ev))!=(ssize_t)sizeof(ev)){fprintf(stderr,"uinput write failed: %s\n",strerror(errno));atomic_store(&quitting,1);break;}
        }
        emit_event(d->fd,EV_SYN,SYN_REPORT,0);
    }
    release_devices();free(frames);frames=NULL;frame_count=frame_cap=0;
    double elapsed=now_sec()-start;printf("[+] Replay of phase %d finished in %.2fs%s.\n",phase,elapsed,stopped?" (stopped by phase button)":"");
    char log[96];snprintf(log,sizeof(log),"PHASE %d END",phase);set_overlay("WAITING",log);overlay_draw();
    return elapsed;
}

static int wait_trigger(Template *templates,int count,double target,int phase) {
    if(!count){fprintf(stderr,"No trigger images found for phase %d.\n",phase);return -1;}
    printf("\n[+] Waiting for Phase %d trigger (target %.2f)...\n",phase,target);fflush(stdout);
    double next_log=0,peak=0,skip_deadline=0;Image screen={0};
    while(!atomic_load(&quitting)){
        if(poll_escape()){atomic_store(&quitting,1);break;}
        if(atomic_load(&retry_event))return -1;
        double now=now_sec();
        if(atomic_load(&skip_event)&&skip_deadline==0)skip_deadline=now+1.0;
        if(skip_deadline>0&&now>=skip_deadline){printf("\n[!] Skip fallback: phase %d trigger not found after 1s; starting its macro.\n",phase);return 1;}
        if(!capture_gray(&screen)){
            double s=find_trigger(templates,count,&screen);if(s>peak)peak=s;
            if(s>=target){printf("\n[+] Phase %d gameplay trigger detected (score %.3f).\n",phase,s);return 1;}
        }
        if(now>=next_log){printf("\r >> [WAITING FOR %d] Peak %.3f / %.2f",phase,peak,target);fflush(stdout);next_log=now+0.5;overlay_draw();}
        usleep(20000);
    }
    return -1;
}

static void show_loop_result(int n,double d1,double d2) {
    char result[16];pthread_mutex_lock(&overlay_lock);snprintf(result,sizeof(result),"%s",mission_result);pthread_mutex_unlock(&overlay_lock);
    printf("\n============================================================\n LOOP #%d — MISSION %s\n Phase 1: %.2fs | Phase 2: %.2fs | Total: %.2fs\n============================================================\n",n,result,d1,d2,d1+d2);fflush(stdout);
}

static int init_capture(void) {
    display_main=XOpenDisplay(NULL);if(!display_main){fprintf(stderr,"Cannot open X11 DISPLAY.\n");return -1;}
    screen_w=DisplayWidth(display_main,DefaultScreen(display_main));screen_h=DisplayHeight(display_main,DefaultScreen(display_main));capture_w=screen_w;capture_h=screen_h;
    XShmQueryExtension(display_main);
    capture_image=XShmCreateImage(display_main,DefaultVisual(display_main,DefaultScreen(display_main)),DefaultDepth(display_main,DefaultScreen(display_main)),ZPixmap,NULL,&capture_shm,capture_w,capture_h);
    if(!capture_image)return -1;
    capture_shm.shmid=shmget(IPC_PRIVATE,(size_t)capture_image->bytes_per_line*capture_h,IPC_CREAT|0600);if(capture_shm.shmid<0)return -1;
    capture_shm.shmaddr=capture_image->data=shmat(capture_shm.shmid,NULL,0);capture_shm.readOnly=False;
    if(!XShmAttach(display_main,&capture_shm))return -1;
    XSync(display_main,False);shmctl(capture_shm.shmid,IPC_RMID,0);
    screen_gray=malloc((size_t)capture_w*capture_h);if(!screen_gray)return -1;
    overlay_init();return 0;
}

static void cleanup(void){
    atomic_store(&quitting,1);if(watcher_running)pthread_join(watcher_thread,NULL);release_devices();
    if(click_fd>=0){ioctl(click_fd,UI_DEV_DESTROY);close(click_fd);click_fd=-1;}
    if(display_main){if(overlay_win)XDestroyWindow(display_main,overlay_win);if(capture_image){XShmDetach(display_main,&capture_shm);XDestroyImage(capture_image);shmdt(capture_shm.shmaddr);}XCloseDisplay(display_main);}
    free(screen_gray);free(screen_big.p);free(screen_half.p);
}

static void on_signal(int s){(void)s;atomic_store(&quitting,1);}

static void usage(const char *p){fprintf(stderr,"Usage: %s [phase1.yml phase2.yml] [--threshold 0.80] [--one-shot]\n",p);}

static const char *recording_path(const char *arg, char *buf, size_t size) {
    if(strchr(arg,'/')||strstr(arg,".yml")||strstr(arg,".yaml")){snprintf(buf,size,"%s",arg);return buf;}
    snprintf(buf,size,"data/recordings/%s.yml",arg);return buf;
}

int main(int argc,char **argv){
    const char *p1="data/recordings/1.yml",*p2="data/recordings/2.yml";int positional=0;char p1buf[MAX_PATH_LEN],p2buf[MAX_PATH_LEN];
    for(int i=1;i<argc;i++){
        if(!strcmp(argv[i],"--one-shot")){one_shot=1;continue;}
        if(!strcmp(argv[i],"--threshold")&&i+1<argc){threshold=strtod(argv[++i],NULL);continue;}
        if(!strcmp(argv[i],"--help")||!strcmp(argv[i],"-h")){usage(argv[0]);return 0;}
        if(argv[i][0]=='-'){usage(argv[0]);return 2;}
        if(positional++==0)p1=recording_path(argv[i],p1buf,sizeof(p1buf));else if(positional==2)p2=recording_path(argv[i],p2buf,sizeof(p2buf));else{usage(argv[0]);return 2;}
    }
    signal(SIGINT,on_signal);signal(SIGTERM,on_signal);XInitThreads();
    if(init_capture()){fprintf(stderr,"Failed to initialize X11 screen capture.\n");cleanup();return 1;}
    load_dir(p1_templates,&p1_count,"data/start/1");load_dir(p2_templates,&p2_count,"data/start/2");
    load_template(&retry_t,"data/retry.png");load_template(&skip_t,"data/skip.png");load_template(&heal_t,"data/heal.png");
    load_template(&completed_t,"data/completed.png");load_template(&failed_t,"data/failed.png");
    printf("AOTR C Player — native C implementation (no Python runtime)\nPhase 1: %s (%d triggers, threshold %.2f)\nPhase 2: %s (%d triggers, threshold 0.70)\nSkip click: (%d,%d); Retry click: (%d,%d)\nPress Esc to stop.\n",p1,p1_count,threshold,p2,p2_count,skip_x,skip_y,retry_x,retry_y);
    if(!p1_count||!p2_count||!retry_t.image.p||!skip_t.image.p){fprintf(stderr,"Required trigger/button PNG/JPEG files are missing.\n");cleanup();return 1;}
    click_fd=create_click_device();if(click_fd<0)fprintf(stderr,"[!] Kernel click device unavailable (%s); using XTest clicks.\n",strerror(errno));
    atomic_store(&quitting,0);
    if(pthread_create(&watcher_thread,NULL,watch_buttons,NULL)==0)watcher_running=1;else{fprintf(stderr,"Could not start button watcher.\n");cleanup();return 1;}
    double total_start=now_sec();
    while(!atomic_load(&quitting)){
        atomic_store(&retry_event,0);atomic_store(&skip_event,0);pthread_mutex_lock(&overlay_lock);snprintf(mission_result,sizeof(mission_result),"UNKNOWN");pthread_mutex_unlock(&overlay_lock);set_overlay("WAIT PHASE 1","");
        if(wait_trigger(p1_templates,p1_count,threshold,1)<0) {if(atomic_load(&retry_event))continue;break;}
        double d1=replay_macro(p1,1);if(atomic_load(&quitting))break;
        if(atomic_load(&retry_event)){show_loop_result(++loops_done,d1,0);continue;}
        set_overlay("WAIT PHASE 2","");overlay_draw();if(wait_trigger(p2_templates,p2_count,0.70,2)<0){if(atomic_load(&retry_event)){show_loop_result(++loops_done,d1,0);continue;}break;}
        double d2=replay_macro(p2,2);if(atomic_load(&quitting))break;
        set_overlay("WAIT RETRY","");overlay_draw();printf("\n[MATCH END] Waiting for Retry detection; next cycle restarts at Phase 1.\n");
        double next_overlay=0;while(!atomic_load(&quitting)&&!atomic_load(&retry_event)){poll_escape();if(now_sec()>=next_overlay){overlay_draw();next_overlay=now_sec()+0.1;}usleep(20000);}
        if(atomic_load(&quitting))break;
        loops_done++;show_loop_result(loops_done,d1,d2);
        atomic_store(&retry_event,0);atomic_store(&skip_event,0);
        if(one_shot)break;
        (void)total_start;
    }
    printf("\nSession ended. Loops completed: %d\n",loops_done);cleanup();return 0;
}
