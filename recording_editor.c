#define _GNU_SOURCE
#include <gtk/gtk.h>
#include <cairo.h>
#include <linux/input.h>
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

typedef struct {
    size_t begin, end;
    int64_t stamp_us;
} FrameMark;

typedef struct {
    int64_t stamp_us;
    int code, value, lane;
    size_t order;
    char name[32];
} KeyMark;

typedef struct {
    char *path;
    char *data;
    char *saved;
    char *undo;
    FrameMark *frames;
    size_t frame_count;
    KeyMark *keys;
    size_t key_count;
    size_t key_capacity;
    int key_codes[KEY_MAX + 1];
    size_t lane_count;
    int64_t duration_us;
    int dirty;
} Document;

typedef struct {
    Document doc;
    GtkWidget *window;
    GtkWidget *area;
    GtkWidget *play_button;
    GtkWidget *event_view;
    GtkWidget *playback_label;
    GtkListStore *event_store;
    GtkWidget *start_spin;
    GtkWidget *end_spin;
    GtkWidget *status;
    double select_start;
    double select_end;
    double drag_anchor;
    int dragging;
    int setting_spins;
    int previewing;
    double preview_from;
    double preview_to;
    gint64 preview_clock_us;
    guint preview_source;
    size_t preview_key_index;
    int preview_active_code;
    double preview_position;
    unsigned char preview_keys[KEY_MAX + 1];
} Editor;

static Editor app;

enum { COL_TIME, COL_KEY, COL_ACTION, COL_COUNT };

static int key_mark_cmp(const void *a,const void *b) {
    const KeyMark *ka=a,*kb=b;
    if(ka->stamp_us<kb->stamp_us)return -1;
    if(ka->stamp_us>kb->stamp_us)return 1;
    return ka->order<kb->order?-1:ka->order>kb->order;
}

static void refresh_event_list(Editor *e);

static char *read_file(const char *path, size_t *length) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long n = ftell(f);
    if (n < 0 || fseek(f, 0, SEEK_SET) != 0) { fclose(f); return NULL; }
    char *data = malloc((size_t)n + 1);
    if (!data) { fclose(f); return NULL; }
    size_t got = fread(data, 1, (size_t)n, f);
    fclose(f);
    if (got != (size_t)n) { free(data); return NULL; }
    data[got] = '\0';
    if (length) *length = got;
    return data;
}

static int is_frame_marker(const char *line, size_t len) {
    return len >= 10 && memcmp(line, "  - evdev:", 10) == 0;
}

static int is_device_marker(const char *line, size_t len) {
    return len >= 7 && memcmp(line, "- node:", 7) == 0;
}

static int parse_event_line(const char *line, size_t len, int *sec, int *usec,
                            int *type, int *code, int *value) {
    const char *dash = memchr(line, '-', len);
    const char *bracket = dash ? memchr(dash, '[', (size_t)(line + len - dash)) : NULL;
    if (!bracket) return 0;
    return sscanf(bracket + 1, " %d , %d , %d , %d , %d", sec, usec, type, code, value) == 5;
}

static int parse_frame_stamp(const char *data, size_t begin, size_t end,
                             int64_t *stamp_us) {
    size_t p = begin;
    int found = 0;
    while (p < end) {
        size_t q = p;
        while (q < end && data[q] != '\n') q++;
        int sec, usec, type, code, value;
        if (parse_event_line(data + p, q - p, &sec, &usec, &type, &code, &value)) {
            if (!found) {
                *stamp_us = (int64_t)sec * 1000000 + usec;
                found = 1;
            }
            (void)type; (void)code; (void)value;
        }
        p = q < end ? q + 1 : end;
    }
    return found;
}

static void clear_index(Document *d) {
    free(d->frames); d->frames = NULL; d->frame_count = 0;
    free(d->keys); d->keys = NULL; d->key_count = d->key_capacity = 0;
    memset(d->key_codes, 0, sizeof(d->key_codes)); d->lane_count = 0;
    d->duration_us = 0;
}

static void key_name_from_line(const char *line, size_t len, int code, char out[32]) {
    const char *p = NULL;
    for (size_t i = 0; i + 4 < len; i++) {
        if (!memcmp(line + i, "KEY_", 4)) { p = line + i + 4; break; }
    }
    if (p) {
        size_t n=0;
        while ((size_t)(p-line)<len && n<31 &&
               ((*p>='A'&&*p<='Z')||(*p>='0'&&*p<='9')||*p=='_')) out[n++]=*p++;
        out[n]='\0';
        if(n)return;
    }
    snprintf(out,32,"KEY%d",code);
}

static int append_key(Document *d, int64_t stamp, int code, int value,
                      const char *line, size_t len) {
    if (d->key_count == d->key_capacity) {
        size_t next_capacity=d->key_capacity?d->key_capacity*2:512;
        KeyMark *next=realloc(d->keys,next_capacity*sizeof(*next));
        if(!next)return -1;
        d->keys=next;d->key_capacity=next_capacity;
    }
    if(code<0||code>KEY_MAX)return 0;
    if(!d->key_codes[code]){d->key_codes[code]=(int)++d->lane_count;}
    KeyMark *m=&d->keys[d->key_count++];
    m->stamp_us=stamp;m->code=code;m->value=value;m->lane=d->key_codes[code]-1;m->order=d->key_count-1;
    key_name_from_line(line,len,code,m->name);
    return 0;
}

static int rebuild_index(Document *d) {
    clear_index(d);
    size_t size = strlen(d->data);
    size_t p = 0, capacity = 0;
    while (p < size) {
        size_t line_end = p;
        while (line_end < size && d->data[line_end] != '\n') line_end++;
        if (!is_frame_marker(d->data + p, line_end - p)) {
            p = line_end < size ? line_end + 1 : size;
            continue;
        }
        size_t end = line_end < size ? line_end + 1 : size;
        while (end < size) {
            size_t next_end = end;
            while (next_end < size && d->data[next_end] != '\n') next_end++;
            if (is_frame_marker(d->data + end, next_end - end) ||
                is_device_marker(d->data + end, next_end - end)) break;
            end = next_end < size ? next_end + 1 : size;
        }
        FrameMark mark;
        if (parse_frame_stamp(d->data, p, end, &mark.stamp_us)) {
            mark.begin = p; mark.end = end;
            if (d->frame_count == capacity) {
                size_t next_capacity = capacity ? capacity * 2 : 1024;
                FrameMark *next = realloc(d->frames, next_capacity * sizeof(*next));
                if (!next) return -1;
                d->frames = next; capacity = next_capacity;
            }
            d->frames[d->frame_count++] = mark;
            if (mark.stamp_us > d->duration_us) d->duration_us = mark.stamp_us;
            size_t q=p;
            while(q<end){size_t le=q;while(le<end&&d->data[le]!='\n')le++;int sec,usec,type,code,value;
                if(parse_event_line(d->data+q,le-q,&sec,&usec,&type,&code,&value)&&type==EV_KEY&&code<BTN_MISC&&value>=0&&value<=2){
                    int64_t stamp=(int64_t)sec*1000000+usec;
                    if(append_key(d,stamp,code,value,d->data+q,le-q)!=0)return -1;
                }
                q=le<end?le+1:end;
            }
        }
        p = end;
    }
    qsort(d->keys,d->key_count,sizeof(*d->keys),key_mark_cmp);
    d->dirty = d->saved && strcmp(d->data, d->saved) != 0;
    refresh_event_list(&app);
    return 0;
}

static int open_document(Editor *e, const char *path) {
    size_t len = 0;
    char *contents = read_file(path, &len);
    if (!contents) return -1;
    free(e->doc.path); free(e->doc.data); free(e->doc.saved); free(e->doc.undo);
    clear_index(&e->doc);
    e->doc.path = strdup(path);
    e->doc.data = contents;
    e->doc.saved = strdup(contents);
    e->doc.undo = NULL;
    if (!e->doc.path || !e->doc.saved || rebuild_index(&e->doc) != 0) return -1;
    e->select_start = 0;
    e->select_end = 0;
    return 0;
}

static void update_status(Editor *e, const char *extra) {
    double duration = e->doc.duration_us / 1000000.0;
    if (e->start_spin) {
        e->setting_spins = 1;
        gtk_adjustment_set_upper(gtk_spin_button_get_adjustment(GTK_SPIN_BUTTON(e->start_spin)), duration > 0 ? duration : 1);
        gtk_adjustment_set_upper(gtk_spin_button_get_adjustment(GTK_SPIN_BUTTON(e->end_spin)), duration > 0 ? duration : 1);
        e->setting_spins = 0;
    }
    char text[512];
    snprintf(text, sizeof(text), "%s   |   %.2f s   |   %zu keyboard events%s%s",
             e->doc.path ? e->doc.path : "No recording loaded", duration,
             e->doc.key_count, e->doc.dirty ? "   |   unsaved edits" : "",
             extra ? extra : "");
    gtk_label_set_text(GTK_LABEL(e->status), text);
    gtk_widget_set_sensitive(e->area, e->doc.path != NULL);
    gtk_widget_queue_draw(e->area);
}

static const char *action_name(int value) {
    if(value==1)return "DOWN";
    if(value==2)return "REPEAT";
    return "UP";
}

static void refresh_event_list(Editor *e) {
    if(!e->event_store)return;
    gtk_list_store_clear(e->event_store);
    for(size_t i=0;i<e->doc.key_count;i++){
        KeyMark *k=&e->doc.keys[i];GtkTreeIter iter;char time_text[32],key_text[40];
        snprintf(time_text,sizeof(time_text),"%.6f",k->stamp_us/1000000.0);
        snprintf(key_text,sizeof(key_text),"KEY_%s",k->name);
        gtk_list_store_append(e->event_store,&iter);
        gtk_list_store_set(e->event_store,&iter,COL_TIME,time_text,COL_KEY,key_text,COL_ACTION,action_name(k->value),-1);
    }
}

static double x_to_time(Editor *e, double x, double width) {
    const double left=88,right=10;
    double plot=width-left-right;
    if (plot < 1 || e->doc.duration_us <= 0) return 0;
    x-=left;
    if (x < 0) x = 0;
    if (x > plot) x = plot;
    return x / plot * (e->doc.duration_us / 1000000.0);
}

static double time_to_x(Editor *e, double t, double width) {
    if (e->doc.duration_us <= 0) return 0;
    return t / (e->doc.duration_us / 1000000.0) * width;
}

static gboolean draw_timeline(GtkWidget *widget, cairo_t *cr, gpointer data) {
    Editor *e = data;
    GtkAllocation allocation;
    gtk_widget_get_allocation(widget, &allocation);
    double w = allocation.width, h = allocation.height;
    cairo_set_source_rgb(cr, 0.08, 0.10, 0.13); cairo_paint(cr);
    if (!e->doc.path || e->doc.duration_us <= 0) return FALSE;

    double duration = e->doc.duration_us / 1000000.0;
    const double left=88,right=10,top=28,bottom=24;
    double plot_w=w-left-right,plot_h=h-top-bottom;
    double sx=left+time_to_x(e,e->select_start,plot_w),ex=left+time_to_x(e,e->select_end,plot_w);
    if (e->select_end > e->select_start) {
        cairo_set_source_rgba(cr, 0.25, 0.55, 0.95, 0.23);
        cairo_rectangle(cr, sx, 0, ex - sx, h); cairo_fill(cr);
    }
    double lane_h=e->doc.lane_count?plot_h/e->doc.lane_count:plot_h;
    if(lane_h<12)lane_h=12;
    cairo_select_font_face(cr,"Sans",CAIRO_FONT_SLANT_NORMAL,CAIRO_FONT_WEIGHT_NORMAL);cairo_set_font_size(cr,11);
    for(size_t lane=0;lane<e->doc.lane_count;lane++){
        double y=top+lane*lane_h;
        cairo_set_source_rgba(cr,1,1,1,0.08);cairo_move_to(cr,left,y+lane_h);cairo_line_to(cr,w-right,y+lane_h);cairo_stroke(cr);
        const char *label="";for(size_t k=0;k<e->doc.key_count;k++)if(e->doc.keys[k].lane==(int)lane){label=e->doc.keys[k].name;break;}
        cairo_set_source_rgb(cr,0.82,0.84,0.88);cairo_move_to(cr,4,y+lane_h*0.72);cairo_show_text(cr,label);
        int code=-1;for(size_t k=0;k<e->doc.key_count;k++)if(e->doc.keys[k].lane==(int)lane){code=e->doc.keys[k].code;break;}
        if(code>=0&&e->preview_keys[code]){cairo_set_source_rgba(cr,0.2,0.8,0.4,0.16);cairo_rectangle(cr,left,y,plot_w,lane_h);cairo_fill(cr);}
    }
    cairo_set_source_rgba(cr, 1, 1, 1, 0.22); cairo_set_line_width(cr, 1);
    int tick = duration < 20 ? 1 : duration < 90 ? 5 : 10;
    for (double t = 0; t <= duration; t += tick) {
        double x = left+time_to_x(e, t, plot_w);
        cairo_move_to(cr, x, top); cairo_line_to(cr, x, h-bottom); cairo_stroke(cr);
        char label[32]; snprintf(label, sizeof(label), "%.0fs", t);
        cairo_set_source_rgb(cr, 0.78, 0.80, 0.84);
        cairo_move_to(cr, x + 3, h - 5); cairo_show_text(cr, label);
    }
    for(size_t i=0;i<e->doc.key_count;i++){
        KeyMark *k=&e->doc.keys[i];double x=left+time_to_x(e,k->stamp_us/1000000.0,plot_w);double y=top+(k->lane+0.5)*lane_h;
        if(k->value==1)cairo_set_source_rgb(cr,1.0,0.62,0.27);
        else if(k->value==2)cairo_set_source_rgb(cr,0.78,0.52,1.0);
        else cairo_set_source_rgb(cr,0.55,0.64,0.73);
        cairo_rectangle(cr,x-2,y-4,4,8);cairo_fill(cr);
    }
    if(e->previewing){double x=left+time_to_x(e,e->preview_position,plot_w);cairo_set_source_rgb(cr,1,0.3,0.3);cairo_set_line_width(cr,2);cairo_move_to(cr,x,top);cairo_line_to(cr,x,h-bottom);cairo_stroke(cr);}
    if (e->select_end > e->select_start) {
        cairo_set_source_rgb(cr, 0.7, 0.85, 1); cairo_set_line_width(cr, 2);
        cairo_move_to(cr, sx, 0); cairo_line_to(cr, sx, h); cairo_stroke(cr);
        cairo_move_to(cr, ex, 0); cairo_line_to(cr, ex, h); cairo_stroke(cr);
    }
    return FALSE;
}

static void set_selection(Editor *e, double start, double end) {
    double duration = e->doc.duration_us / 1000000.0;
    if (start < 0) start = 0;
    if (end < 0) end = 0;
    if (start > duration) start = duration;
    if (end > duration) end = duration;
    if (end < start) { double t = start; start = end; end = t; }
    e->select_start = start; e->select_end = end;
    e->setting_spins = 1;
    gtk_spin_button_set_value(GTK_SPIN_BUTTON(e->start_spin), start);
    gtk_spin_button_set_value(GTK_SPIN_BUTTON(e->end_spin), end);
    e->setting_spins = 0;
    gtk_widget_queue_draw(e->area);
}

static gboolean button_press(GtkWidget *widget, GdkEventButton *event, gpointer data) {
    (void)data;
    if (event->button != 1 || !app.doc.path) return FALSE;
    GtkAllocation a; gtk_widget_get_allocation(widget, &a);
    app.dragging = 1;
    double t = x_to_time(&app, event->x, a.width);
    app.drag_anchor = t;
    set_selection(&app, t, t);
    return TRUE;
}

static gboolean pointer_move(GtkWidget *widget, GdkEventMotion *event, gpointer data) {
    (void)data;
    if (!app.dragging) return FALSE;
    GtkAllocation a; gtk_widget_get_allocation(widget, &a);
    set_selection(&app, app.drag_anchor, x_to_time(&app, event->x, a.width));
    return TRUE;
}

static gboolean button_release(GtkWidget *widget, GdkEventButton *event, gpointer data) {
    (void)widget; (void)data;
    if (event->button == 1) app.dragging = 0;
    return FALSE;
}

static void spin_changed(GtkSpinButton *spin, gpointer data) {
    (void)spin;
    Editor *e = data;
    if (e->setting_spins) return;
    double a = gtk_spin_button_get_value(GTK_SPIN_BUTTON(e->start_spin));
    double b = gtk_spin_button_get_value(GTK_SPIN_BUTTON(e->end_spin));
    set_selection(e, a, b);
}

static void stop_preview(Editor *e, const char *message) {
    if(!e->previewing)return;
    e->previewing=0;
    if(e->preview_source){g_source_remove(e->preview_source);e->preview_source=0;}
    gtk_button_set_label(GTK_BUTTON(e->play_button),"Play");
    memset(e->preview_keys,0,sizeof(e->preview_keys));
    e->preview_active_code=-1;
    if(message)gtk_label_set_text(GTK_LABEL(e->playback_label),message);
    gtk_widget_queue_draw(e->area);
}

static gboolean preview_tick(gpointer data) {
    Editor *e=data;
    double elapsed=(g_get_monotonic_time()-e->preview_clock_us)/1000000.0;
    e->preview_position=e->preview_from+elapsed;
    if(e->preview_position>=e->preview_to)e->preview_position=e->preview_to;
    size_t last=SIZE_MAX;
    while(e->preview_key_index<e->doc.key_count&&e->doc.keys[e->preview_key_index].stamp_us<=llround(e->preview_position*1000000.0)){
        KeyMark *k=&e->doc.keys[e->preview_key_index];
        if(k->value==1)e->preview_keys[k->code]=1;
        else if(k->value==0)e->preview_keys[k->code]=0;
        e->preview_active_code=k->code;
        last=e->preview_key_index;
        e->preview_key_index++;
    }
    char label[128];
    if(last<e->doc.key_count){KeyMark *k=&e->doc.keys[last];snprintf(label,sizeof(label),"%.3f s   KEY_%s %s",e->preview_position,k->name,action_name(k->value));}
    else snprintf(label,sizeof(label),"%.3f s",e->preview_position);
    gtk_label_set_text(GTK_LABEL(e->playback_label),label);
    if(last<e->doc.key_count){
        GtkTreePath *path=gtk_tree_path_new_from_indices((int)last,-1);
        gtk_tree_view_set_cursor(GTK_TREE_VIEW(e->event_view),path,NULL,FALSE);
        gtk_tree_view_scroll_to_cell(GTK_TREE_VIEW(e->event_view),path,NULL,TRUE,0.5,0.0);
        gtk_tree_path_free(path);
    }
    gtk_widget_queue_draw(e->area);
    if(e->preview_position>=e->preview_to){e->preview_source=0;stop_preview(e,"Preview finished");return G_SOURCE_REMOVE;}
    return G_SOURCE_CONTINUE;
}

static void play_clicked(GtkButton *button, gpointer data) {
    (void)button;
    Editor *e=data;
    if(e->previewing){stop_preview(e,"Preview stopped");return;}
    if(!e->doc.key_count||e->doc.duration_us<=0){gtk_label_set_text(GTK_LABEL(e->playback_label),"No keyboard events to preview");return;}
    e->preview_from=e->select_end>e->select_start?e->select_start:0.0;
    e->preview_to=e->select_end>e->select_start?e->select_end:e->doc.duration_us/1000000.0;
    memset(e->preview_keys,0,sizeof(e->preview_keys));e->preview_active_code=-1;e->preview_key_index=0;
    while(e->preview_key_index<e->doc.key_count&&e->doc.keys[e->preview_key_index].stamp_us<e->preview_from*1000000.0){
        KeyMark *k=&e->doc.keys[e->preview_key_index++];if(k->value==1)e->preview_keys[k->code]=1;else if(k->value==0)e->preview_keys[k->code]=0;
    }
    e->preview_position=e->preview_from;e->preview_clock_us=g_get_monotonic_time();e->previewing=1;
    gtk_button_set_label(GTK_BUTTON(e->play_button),"Stop");
    gtk_label_set_text(GTK_LABEL(e->playback_label),"Previewing keyboard events (visual only)");
    e->preview_source=g_timeout_add(15,preview_tick,e);gtk_widget_queue_draw(e->area);
}

static char *rewrite_event_line(const char *line, size_t len, int64_t delta_us,
                               int value_delta) {
    const char *bracket = memchr(line, '[', len);
    if (!bracket) return strndup(line, len);
    int sec, usec, type, code, value, consumed;
    if (sscanf(bracket + 1, " %d , %d , %d , %d , %d%n", &sec, &usec, &type, &code, &value, &consumed) != 5) return strndup(line, len);
    const char *rest = bracket + 1 + consumed;
    int64_t total = (int64_t)sec * 1000000 + usec - delta_us;
    if (total < 0) total = 0;
    int64_t new_sec = total / 1000000, new_usec = total % 1000000;
    size_t prefix = (size_t)(bracket + 1 - line);
    size_t suffix = len - (size_t)(rest - line);
    size_t cap = prefix + 80 + suffix + 1;
    char *out = malloc(cap);
    if (!out) return NULL;
    snprintf(out, cap, "%.*s %3lld, %6lld, %3d, %3d, %8d%.*s",
             (int)prefix, line, (long long)new_sec, (long long)new_usec,
             type, code, value + value_delta, (int)suffix, rest);
    return out;
}

static int append_bytes(char **out,size_t *used,size_t *capacity,const char *src,size_t n) {
    if(*used+n+1>*capacity){size_t cap=(*used+n+1)*2;char*p=realloc(*out,cap);if(!p)return -1;*out=p;*capacity=cap;}
    memcpy(*out+*used,src,n);*used+=n;return 0;
}

static int frame_mouse_motion(const char *data,size_t begin,size_t end,int64_t *dx,int64_t *dy) {
    size_t p=begin;int found=0;*dx=*dy=0;
    while(p<end){size_t le=p;while(le<end&&data[le]!='\n')le++;int s,u,t,c,v;
        if(parse_event_line(data+p,le-p,&s,&u,&t,&c,&v)&&t==EV_REL){
            if(c==REL_X){*dx+=v;found=1;}else if(c==REL_Y){*dy+=v;found=1;}
        }
        p=le<end?le+1:end;
    }
    return found;
}

static double smoothstep(double u) {
    if(u<0)u=0;
    if(u>1)u=1;
    return u*u*(3.0-2.0*u);
}

static int append_rel_compensation(char **out,size_t *used,size_t *capacity,
                                   int64_t stamp_us,int code,int value) {
    int64_t sec=stamp_us/1000000,usec=stamp_us%1000000;
    char line[160];int n=snprintf(line,sizeof(line),"    - [ %3lld, %6lld,   2, %3d, %8d] # EV_REL / %s (ripple-cut compensation)\n",
        (long long)sec,(long long)usec,code,value,code==REL_X?"REL_X":"REL_Y");
    return append_bytes(out,used,capacity,line,(size_t)n);
}

static int append_shifted_frame(char **out,size_t *used,size_t *capacity,
                                const char *data,size_t begin,size_t end,
                                int64_t shift_us,int64_t add_x,int64_t add_y) {
    int64_t output_stamp=0;parse_frame_stamp(data,begin,end,&output_stamp);output_stamp-=shift_us;if(output_stamp<0)output_stamp=0;
    int x_used=0,y_used=0;
    size_t p=begin;
    while(p<end){size_t le=p;while(le<end&&data[le]!='\n')le++;size_t n=le-p;int s,u,t,c,v;
        if(parse_event_line(data+p,n,&s,&u,&t,&c,&v)){
            int bump=0;
            if(t==EV_REL&&c==REL_X&&!x_used){bump=(int)add_x;x_used=1;}
            else if(t==EV_REL&&c==REL_Y&&!y_used){bump=(int)add_y;y_used=1;}
            if(t==EV_SYN&&c==SYN_REPORT){
                if(add_x&&!x_used){if(append_rel_compensation(out,used,capacity,output_stamp,REL_X,(int)add_x))return -1;x_used=1;}
                if(add_y&&!y_used){if(append_rel_compensation(out,used,capacity,output_stamp,REL_Y,(int)add_y))return -1;y_used=1;}
            }
            char *line=rewrite_event_line(data+p,n,shift_us,bump);if(!line)return -1;
            int r=append_bytes(out,used,capacity,line,strlen(line));free(line);if(r)return -1;
        }else if(append_bytes(out,used,capacity,data+p,n))return -1;
        if(le<end&&append_bytes(out,used,capacity,"\n",1))return -1;
        p=le<end?le+1:end;
    }
    if(add_x&&!x_used&&append_rel_compensation(out,used,capacity,output_stamp,REL_X,(int)add_x))return -1;
    if(add_y&&!y_used&&append_rel_compensation(out,used,capacity,output_stamp,REL_Y,(int)add_y))return -1;
    return 0;
}

static char *ripple_cut(const char *data, int64_t start_us, int64_t end_us,
                        size_t *removed,int64_t *mouse_dx,int64_t *mouse_dy,
                        int *corrected_mouse) {
    if (end_us <= start_us) return NULL;
    size_t size = strlen(data), cursor = 0;
    int64_t cut_dx=0,cut_dy=0;
    size_t scan=0;
    while(scan<size){size_t le=scan;while(le<size&&data[le]!='\n')le++;
        if(!is_frame_marker(data+scan,le-scan)){scan=le<size?le+1:size;continue;}
        size_t stop=le<size?le+1:size;
        while(stop<size){size_t ne=stop;while(ne<size&&data[ne]!='\n')ne++;if(is_frame_marker(data+stop,ne-stop)||is_device_marker(data+stop,ne-stop))break;stop=ne<size?ne+1:size;}
        int64_t stamp=0,dx=0,dy=0;
        if(parse_frame_stamp(data,scan,stop,&stamp)&&stamp>=start_us&&stamp<end_us)frame_mouse_motion(data,scan,stop,&dx,&dy),cut_dx+=dx,cut_dy+=dy;
        scan=stop;
    }
    if(mouse_dx)*mouse_dx=cut_dx;
    if(mouse_dy)*mouse_dy=cut_dy;
    if(corrected_mouse)*corrected_mouse=0;
    size_t cap = size + 1, used = 0;
    char *out = malloc(cap);
    if (!out) return NULL;
    size_t p = 0; *removed = 0;int64_t emitted_x=0,emitted_y=0;const int64_t blend_us=500000;
    while (p < size) {
        size_t le = p; while (le < size && data[le] != '\n') le++;
        if (!is_frame_marker(data + p, le - p)) { p = le < size ? le + 1 : size; continue; }
        size_t end = le < size ? le + 1 : size;
        while (end < size) {
            size_t ne = end; while (ne < size && data[ne] != '\n') ne++;
            if (is_frame_marker(data + end, ne - end) || is_device_marker(data + end, ne - end)) break;
            end = ne < size ? ne + 1 : size;
        }
        int64_t stamp = 0;
        int have_stamp = parse_frame_stamp(data, p, end, &stamp);
        if (!have_stamp || stamp < start_us) {
            size_t n = end - cursor;
            if (used + n + 1 > cap) { cap = (used + n + 1) * 2; char *q = realloc(out, cap); if (!q) { free(out); return NULL; } out=q; }
            memcpy(out + used, data + cursor, n); used += n;
        } else if (stamp < end_us) {
            size_t n = p - cursor;
            if (used + n + 1 > cap) { cap = (used + n + 1) * 2; char *q = realloc(out, cap); if (!q) { free(out); return NULL; } out=q; }
            memcpy(out + used, data + cursor, n); used += n;
            (*removed)++;
        } else {
            size_t n = p - cursor;
            if(append_bytes(&out,&used,&cap,data+cursor,n)){free(out);return NULL;}
            int64_t dx=0,dy=0;int has_motion=frame_mouse_motion(data,p,end,&dx,&dy);
            int64_t correction_x=0,correction_y=0;
            if(has_motion&&(cut_dx||cut_dy)){
                /* At the splice, restore the net relative motion deleted with
                 * the cut, blended with a smoothstep over the next 0.5 s. */
                double u=(double)(stamp-end_us)/(double)blend_us;
                int64_t desired_x=llround((double)cut_dx*smoothstep(u));
                int64_t desired_y=llround((double)cut_dy*smoothstep(u));
                correction_x=desired_x-emitted_x;correction_y=desired_y-emitted_y;
                emitted_x=desired_x;emitted_y=desired_y;
                if(corrected_mouse&& (correction_x||correction_y))*corrected_mouse=1;
            }
            if(append_shifted_frame(&out,&used,&cap,data,p,end,end_us-start_us,correction_x,correction_y)){free(out);return NULL;}
        }
        cursor = end;
        p = end;
    }
    if (cursor < size) {
        size_t n = size - cursor;
        if (used + n + 1 > cap) { char *q = realloc(out, used+n+1); if (!q) { free(out); return NULL; } out=q; }
        memcpy(out+used,data+cursor,n);used+=n;
    }
    out[used]='\0';
    return out;
}

static void cut_clicked(GtkButton *button, gpointer data) {
    (void)button;
    Editor *e = data;
    stop_preview(e,"Preview stopped for edit");
    if (!e->doc.data) return;
    int64_t start = (int64_t)llround(e->select_start * 1000000.0);
    int64_t end = (int64_t)llround(e->select_end * 1000000.0);
    if (end <= start) { update_status(e, "   |   drag a non-empty range first"); return; }
    size_t removed = 0;
    int64_t dx=0,dy=0;int corrected=0;
    char *next = ripple_cut(e->doc.data, start, end, &removed,&dx,&dy,&corrected);
    if (!next) { update_status(e, "   |   cut failed"); return; }
    if (!removed) { free(next); update_status(e, "   |   no event frames fell in that range"); return; }
    free(e->doc.undo); e->doc.undo = strdup(e->doc.data);
    free(e->doc.data); e->doc.data = next;
    if (rebuild_index(&e->doc) != 0) { update_status(e, "   |   could not rebuild timeline"); return; }
    double removed_s = (end-start)/1000000.0;
    update_status(e,NULL);
    set_selection(e, start/1000000.0, start/1000000.0);
    char msg[256];
    if(dx||dy){
        if(corrected)snprintf(msg,sizeof(msg),"   |   cut %zu frames; shifted %.3f s; mouse endpoint compensation Δx=%lld Δy=%lld blended over 0.5 s",removed,removed_s,(long long)dx,(long long)dy);
        else snprintf(msg,sizeof(msg),"   |   cut %zu frames; shifted %.3f s; mouse Δx=%lld Δy=%lld but no later mouse-motion frame was available",removed,removed_s,(long long)dx,(long long)dy);
    } else snprintf(msg,sizeof(msg),"   |   cut %zu frames; shifted later events by %.3f s; net mouse motion in cut was zero",removed,removed_s);
    update_status(e,msg);
}

static void undo_clicked(GtkButton *button, gpointer data) {
    (void)button;
    Editor *e=data;if(!e->doc.undo)return;
    free(e->doc.data);e->doc.data=e->doc.undo;e->doc.undo=NULL;
    rebuild_index(&e->doc);update_status(e,NULL);set_selection(e,0,0);update_status(e,"   |   last cut undone");
}

static void reset_clicked(GtkButton *button, gpointer data) {
    (void)button;
    Editor *e=data;if(!e->doc.saved)return;
    free(e->doc.undo);e->doc.undo=strdup(e->doc.data);
    free(e->doc.data);e->doc.data=strdup(e->doc.saved);
    rebuild_index(&e->doc);update_status(e,NULL);set_selection(e,0,0);update_status(e,"   |   reset to last saved file");
}

static char *backup_path(const char *path) {
    size_t n=strlen(path);const char *ext=strrchr(path,'.');
    if(ext&&(!strcmp(ext,".yml")||!strcmp(ext,".yaml"))) {
        size_t stem=(size_t)(ext-path);char *p=malloc(stem+sizeof(".before-edit.yml"));
        if(p){memcpy(p,path,stem);strcpy(p+stem,".before-edit.yml");}return p;
    }
    char *p=malloc(n+sizeof(".before-edit.yml"));if(p){strcpy(p,path);strcat(p,".before-edit.yml");}return p;
}

static int save_document(Document *d) {
    char *bak=backup_path(d->path);if(!bak)return -1;
    int bfd=open(bak,O_WRONLY|O_CREAT|O_EXCL,0644);
    if(bfd>=0){size_t n=strlen(d->saved?d->saved:d->data),off=0;const char*src=d->saved?d->saved:d->data;while(off<n){ssize_t w=write(bfd,src+off,n-off);if(w<=0){close(bfd);free(bak);return -1;}off+=(size_t)w;}fsync(bfd);close(bfd);}
    else if(errno!=EEXIST){free(bak);return -1;}
    free(bak);
    size_t n=strlen(d->data);char *tmp=malloc(strlen(d->path)+16);if(!tmp)return -1;
    sprintf(tmp,"%s.edit-tmp",d->path);
    int fd=open(tmp,O_WRONLY|O_CREAT|O_TRUNC,0644);if(fd<0){free(tmp);return -1;}
    size_t off=0;while(off<n){ssize_t w=write(fd,d->data+off,n-off);if(w<=0){close(fd);unlink(tmp);free(tmp);return -1;}off+=(size_t)w;}
    if(fsync(fd)!=0){close(fd);unlink(tmp);free(tmp);return -1;}close(fd);
    if(rename(tmp,d->path)!=0){unlink(tmp);free(tmp);return -1;}free(tmp);
    free(d->saved);d->saved=strdup(d->data);d->dirty=0;return 0;
}

static void save_clicked(GtkButton *button, gpointer data) {
    (void)button;
    Editor *e=data;if(!e->doc.path||!e->doc.dirty){update_status(e,"   |   no unsaved edits");return;}
    if(save_document(&e->doc)==0)update_status(e,"   |   saved; original copied to .before-edit.yml");
    else {char msg[128];snprintf(msg,sizeof(msg),"   |   save failed: %s",strerror(errno));update_status(e,msg);}
}

static void open_clicked(GtkButton *button, gpointer data) {
    (void)button;
    Editor *e=data;
    stop_preview(e,"Preview stopped");
    if(e->doc.dirty){
        GtkWidget *confirm=gtk_message_dialog_new(GTK_WINDOW(e->window),GTK_DIALOG_MODAL,GTK_MESSAGE_WARNING,GTK_BUTTONS_YES_NO,
            "Discard unsaved cuts and open another recording?");
        int response=gtk_dialog_run(GTK_DIALOG(confirm));gtk_widget_destroy(confirm);
        if(response!=GTK_RESPONSE_YES)return;
    }
    GtkWidget *dialog=gtk_file_chooser_dialog_new("Open input recording",GTK_WINDOW(e->window),GTK_FILE_CHOOSER_ACTION_OPEN,
        "Cancel",GTK_RESPONSE_CANCEL,"Open",GTK_RESPONSE_ACCEPT,NULL);
    GtkFileFilter *filter=gtk_file_filter_new();gtk_file_filter_set_name(filter,"libinput recordings (*.yml, *.yaml)");
    gtk_file_filter_add_pattern(filter,"*.yml");gtk_file_filter_add_pattern(filter,"*.yaml");gtk_file_chooser_add_filter(GTK_FILE_CHOOSER(dialog),filter);
    if(gtk_dialog_run(GTK_DIALOG(dialog))==GTK_RESPONSE_ACCEPT){char *path=gtk_file_chooser_get_filename(GTK_FILE_CHOOSER(dialog));
        if(open_document(e,path)==0)update_status(e,NULL);else update_status(e,"   |   could not open recording");g_free(path);}
    gtk_widget_destroy(dialog);
}

static gboolean delete_window(GtkWidget *widget, GdkEvent *event, gpointer data) {
    (void)event; (void)data;
    if(app.doc.dirty){
        GtkWidget *d=gtk_message_dialog_new(GTK_WINDOW(widget),GTK_DIALOG_MODAL,GTK_MESSAGE_WARNING,GTK_BUTTONS_YES_NO,
            "There are unsaved cuts. Close without saving?");
        int response=gtk_dialog_run(GTK_DIALOG(d));gtk_widget_destroy(d);
        if(response!=GTK_RESPONSE_YES)return TRUE;
    }
    gtk_main_quit();return FALSE;
}

static GtkWidget *make_button(const char *label, GCallback callback) {
    GtkWidget *b=gtk_button_new_with_label(label);g_signal_connect(b,"clicked",callback,&app);return b;
}

static void build_ui(void) {
    app.window=gtk_window_new(GTK_WINDOW_TOPLEVEL);gtk_window_set_title(GTK_WINDOW(app.window),"AOTR Recording Editor — Ripple Cut");
    gtk_window_set_default_size(GTK_WINDOW(app.window),1100,620);gtk_window_set_position(GTK_WINDOW(app.window),GTK_WIN_POS_CENTER);
    g_signal_connect(app.window,"delete-event",G_CALLBACK(delete_window),NULL);
    GtkWidget *root=gtk_box_new(GTK_ORIENTATION_VERTICAL,8);gtk_container_set_border_width(GTK_CONTAINER(root),12);gtk_container_add(GTK_CONTAINER(app.window),root);
    app.area=gtk_drawing_area_new();gtk_widget_set_size_request(app.area,800,300);gtk_widget_add_events(app.area,GDK_BUTTON_PRESS_MASK|GDK_BUTTON_RELEASE_MASK|GDK_POINTER_MOTION_MASK);
    g_signal_connect(app.area,"draw",G_CALLBACK(draw_timeline),&app);g_signal_connect(app.area,"button-press-event",G_CALLBACK(button_press),&app);
    g_signal_connect(app.area,"motion-notify-event",G_CALLBACK(pointer_move),&app);g_signal_connect(app.area,"button-release-event",G_CALLBACK(button_release),&app);
    gtk_box_pack_start(GTK_BOX(root),app.area,TRUE,TRUE,0);
    app.event_store=gtk_list_store_new(COL_COUNT,G_TYPE_STRING,G_TYPE_STRING,G_TYPE_STRING);
    app.event_view=gtk_tree_view_new_with_model(GTK_TREE_MODEL(app.event_store));
    const char *titles[COL_COUNT]={"Time (seconds)","Keyboard input","Action"};
    for(int c=0;c<COL_COUNT;c++){GtkCellRenderer *renderer=gtk_cell_renderer_text_new();GtkTreeViewColumn *column=gtk_tree_view_column_new_with_attributes(titles[c],renderer,"text",c,NULL);gtk_tree_view_append_column(GTK_TREE_VIEW(app.event_view),column);}
    GtkWidget *scroll=gtk_scrolled_window_new(NULL,NULL);gtk_widget_set_size_request(scroll,-1,175);gtk_scrolled_window_set_policy(GTK_SCROLLED_WINDOW(scroll),GTK_POLICY_AUTOMATIC,GTK_POLICY_AUTOMATIC);gtk_container_add(GTK_CONTAINER(scroll),app.event_view);gtk_box_pack_start(GTK_BOX(root),scroll,TRUE,TRUE,0);
    GtkWidget *controls=gtk_box_new(GTK_ORIENTATION_HORIZONTAL,8);gtk_box_pack_start(GTK_BOX(root),controls,FALSE,FALSE,0);
    gtk_box_pack_start(GTK_BOX(controls),make_button("Open…",G_CALLBACK(open_clicked)),FALSE,FALSE,0);
    app.play_button=make_button("Play",G_CALLBACK(play_clicked));gtk_box_pack_start(GTK_BOX(controls),app.play_button,FALSE,FALSE,0);
    gtk_box_pack_start(GTK_BOX(controls),make_button("Undo cut",G_CALLBACK(undo_clicked)),FALSE,FALSE,0);
    gtk_box_pack_start(GTK_BOX(controls),make_button("Reset to saved",G_CALLBACK(reset_clicked)),FALSE,FALSE,0);
    GtkAdjustment *a=gtk_adjustment_new(0,0,1,0.01,0.1,0),*b=gtk_adjustment_new(0,0,1,0.01,0.1,0);
    app.start_spin=gtk_spin_button_new(a,0.01,3);app.end_spin=gtk_spin_button_new(b,0.01,3);
    gtk_box_pack_start(GTK_BOX(controls),gtk_label_new("From (s)"),FALSE,FALSE,0);gtk_box_pack_start(GTK_BOX(controls),app.start_spin,FALSE,FALSE,0);
    gtk_box_pack_start(GTK_BOX(controls),gtk_label_new("To (s)"),FALSE,FALSE,0);gtk_box_pack_start(GTK_BOX(controls),app.end_spin,FALSE,FALSE,0);
    g_signal_connect(app.start_spin,"value-changed",G_CALLBACK(spin_changed),&app);g_signal_connect(app.end_spin,"value-changed",G_CALLBACK(spin_changed),&app);
    gtk_box_pack_start(GTK_BOX(controls),make_button("Cut selection",G_CALLBACK(cut_clicked)),FALSE,FALSE,0);
    gtk_box_pack_start(GTK_BOX(controls),make_button("Save",G_CALLBACK(save_clicked)),FALSE,FALSE,0);
    app.status=gtk_label_new("Open a recording, drag over the timeline, then cut. Later events ripple left.");
    gtk_label_set_xalign(GTK_LABEL(app.status),0);gtk_box_pack_start(GTK_BOX(root),app.status,FALSE,FALSE,0);
    app.playback_label=gtk_label_new("Keyboard preview is visual only; it does not send input to the game.");gtk_label_set_xalign(GTK_LABEL(app.playback_label),0);gtk_box_pack_start(GTK_BOX(root),app.playback_label,FALSE,FALSE,0);
    gtk_widget_show_all(app.window);
}

int main(int argc,char **argv) {
    gtk_init(&argc,&argv);build_ui();
    const char *initial=argc>1?argv[1]:"data/recordings/1.yml";
    if(open_document(&app,initial)==0)update_status(&app,NULL);
    else update_status(&app,"   |   open a .yml/.yaml recording to begin");
    gtk_main();
    free(app.doc.path);free(app.doc.data);free(app.doc.saved);free(app.doc.undo);free(app.doc.frames);
    return 0;
}
