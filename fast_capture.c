/*
 * fast_capture.c — Zero-copy X11 screen capture (XShmGetImage) + XInput2 raw
 * event listener in a native pthread + uinput virtual mouse. Python C extension.
 *
 * Build:  bash build_fast_capture.sh
 *
 * API:
 *   init_capture(w, h, x, y)                             — set up SHM region
 *   grab_gray()                                           — returns uint8[h,w] numpy (zero-copy)
 *   close_capture()                                       — free SHM + display
 *   start_xi2(on_move, on_click, on_scroll [, excl_id])  — start XI2 thread
 *   stop_xi2()                                            — stop thread
 *   uinput_open()                                         — create virtual mouse (needs /dev/uinput rw)
 *   uinput_close()                                        — destroy virtual mouse
 *   uinput_move(dx, dy)                                   — relative mouse motion via kernel
 *   uinput_click(button, pressed)                         — button press/release (1=left,2=mid,3=right)
 *   uinput_scroll(dy)                                     — scroll wheel
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#define NPY_NO_DEPRECATED_API NPY_1_7_API_VERSION
#include <numpy/arrayobject.h>

#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/XShm.h>
#include <X11/extensions/XInput2.h>
#include <sys/shm.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* uinput */
#include <linux/uinput.h>
#include <linux/input.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>

/* ── Capture state ──────────────────────────────────────────────────────────*/
static Display        *g_dpy    = NULL;
static Window          g_root   = 0;
static XShmSegmentInfo g_shm;
static XImage         *g_img    = NULL;
static int g_cap_w, g_cap_h, g_cap_x, g_cap_y, g_bpp = 4;
static uint8_t        *g_gray   = NULL;
static int             g_gray_n = 0;

/* ── XI2 state ──────────────────────────────────────────────────────────────*/
static Display        *g_xi2_dpy = NULL;
static int             g_xi2_op  = 0;
static volatile int    g_xi2_run = 0;
static pthread_t       g_xi2_tid = 0;
static int             g_excl    = 4;
static PyObject       *g_cb_move = NULL, *g_cb_click = NULL, *g_cb_scrl = NULL;

/* ── uinput virtual mouse state ────────────────────────────────────────────*/
static int g_uinput_fd = -1;

static int uinput_emit(unsigned short type, unsigned short code, int value)
{
    struct input_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = type;
    ev.code = code;
    ev.value = value;
    return write(g_uinput_fd, &ev, sizeof(ev)) == (ssize_t)sizeof(ev) ? 0 : -1;
}

static int uinput_sync(void)
{
    return uinput_emit(EV_SYN, SYN_REPORT, 0);
}

static int uinput_button_code(int button)
{
    switch (button) {
        case 1: return BTN_LEFT;
        case 2: return BTN_MIDDLE;
        case 3: return BTN_RIGHT;
        case 8: return BTN_SIDE;
        case 9: return BTN_EXTRA;
        default: return -1;
    }
}

/* ── Screen capture ─────────────────────────────────────────────────────────*/

static PyObject *py_init_capture(PyObject *self, PyObject *args)
{
    int w, h, x, y;
    if (!PyArg_ParseTuple(args, "iiii", &w, &h, &x, &y)) return NULL;

    if (g_img) {
        XShmDetach(g_dpy, &g_shm); XDestroyImage(g_img); shmdt(g_shm.shmaddr); g_img = NULL;
    }
    if (!g_dpy) {
        g_dpy = XOpenDisplay(NULL);
        if (!g_dpy) { PyErr_SetString(PyExc_RuntimeError,"Cannot open display"); return NULL; }
    }
    g_root=DefaultRootWindow(g_dpy); g_cap_w=w; g_cap_h=h; g_cap_x=x; g_cap_y=y;

    Visual *vis=DefaultVisual(g_dpy,DefaultScreen(g_dpy));
    int depth=DefaultDepth(g_dpy,DefaultScreen(g_dpy));
    g_img=XShmCreateImage(g_dpy,vis,depth,ZPixmap,NULL,&g_shm,w,h);
    if(!g_img){PyErr_SetString(PyExc_RuntimeError,"XShmCreateImage failed");return NULL;}
    g_bpp=g_img->bits_per_pixel/8;

    g_shm.shmid=shmget(IPC_PRIVATE,(size_t)g_img->bytes_per_line*h,IPC_CREAT|0600);
    if(g_shm.shmid<0){XDestroyImage(g_img);g_img=NULL;PyErr_SetString(PyExc_RuntimeError,"shmget failed");return NULL;}
    g_shm.shmaddr=g_img->data=(char*)shmat(g_shm.shmid,NULL,0);
    g_shm.readOnly=False;
    XShmAttach(g_dpy,&g_shm); XSync(g_dpy,False);
    shmctl(g_shm.shmid,IPC_RMID,0);

    int n=w*h;
    if(n>g_gray_n){free(g_gray);g_gray=(uint8_t*)malloc(n);g_gray_n=n;}
    Py_RETURN_NONE;
}

static PyObject *py_grab_gray(PyObject *self, PyObject *args)
{
    if(!g_img){PyErr_SetString(PyExc_RuntimeError,"not initialised");return NULL;}
    XShmGetImage(g_dpy,g_root,g_img,g_cap_x,g_cap_y,AllPlanes);

    int n=g_cap_w*g_cap_h;
    const uint8_t *s=(const uint8_t*)g_shm.shmaddr;
    uint8_t *d=g_gray;
    int bpp=g_bpp;

    /* Rec.601 BGRX→gray: compiler auto-vectorises to AVX2 with -O3 -march=native */
    if(bpp==4){
        for(int i=0;i<n;i++,s+=4)
            d[i]=(uint8_t)((29u*s[0]+150u*s[1]+77u*s[2])>>8);
    }else if(bpp==3){
        for(int i=0;i<n;i++,s+=3)
            d[i]=(uint8_t)((29u*s[0]+150u*s[1]+77u*s[2])>>8);
    }else{
        for(int i=0;i<n;i++,s+=bpp) d[i]=s[0];
    }

    npy_intp dims[2]={(npy_intp)g_cap_h,(npy_intp)g_cap_w};
    return PyArray_SimpleNewFromData(2,dims,NPY_UINT8,g_gray);
}

static PyObject *py_close_capture(PyObject *self, PyObject *args)
{
    if(g_img){XShmDetach(g_dpy,&g_shm);XDestroyImage(g_img);shmdt(g_shm.shmaddr);g_img=NULL;}
    if(g_dpy){XCloseDisplay(g_dpy);g_dpy=NULL;}
    free(g_gray);g_gray=NULL;g_gray_n=0;
    Py_RETURN_NONE;
}

/* ── XInput2 listener thread ────────────────────────────────────────────────*/

static void *xi2_fn(void *arg)
{
    (void)arg;
    Display *dpy=g_xi2_dpy;
    Window root=DefaultRootWindow(dpy);

    unsigned char mb[XIMaskLen(XI_LASTEVENT)]; memset(mb,0,sizeof(mb));
    XISetMask(mb,XI_RawMotion);
    XISetMask(mb,XI_RawButtonPress);
    XISetMask(mb,XI_RawButtonRelease);
    XIEventMask em={XIAllMasterDevices,sizeof(mb),mb};
    XISelectEvents(dpy,root,&em,1); XFlush(dpy);

    struct timespec ts={0,500000}; /* 0.5ms poll interval when idle */
    XEvent xev;
    while(g_xi2_run){
        if(!XPending(dpy)){nanosleep(&ts,NULL);continue;}
        XNextEvent(dpy,&xev);
        if(xev.type!=GenericEvent||xev.xcookie.extension!=g_xi2_op) continue;
        if(!XGetEventData(dpy,&xev.xcookie)) continue;

        XIRawEvent *re=(XIRawEvent*)xev.xcookie.data;
        int et=xev.xcookie.evtype;

        /* Filter synthetic XTest events */
        if(g_excl>=0 && re->sourceid==g_excl){XFreeEventData(dpy,&xev.xcookie);continue;}

        if(et==XI_RawMotion){
            double dx=0,dy=0; int vi=0;
            for(int b=0;b<re->valuators.mask_len*8;b++){
                if(XIMaskIsSet(re->valuators.mask,b)){
                    double v=re->raw_values[vi++];
                    if(b==0)dx=v; else if(b==1)dy=v;
                }
            }
            if((dx||dy)&&g_cb_move){
                PyGILState_STATE gs=PyGILState_Ensure();
                PyObject *r=PyObject_CallFunction(g_cb_move,"dd",dx,dy);
                Py_XDECREF(r); if(PyErr_Occurred())PyErr_Clear();
                PyGILState_Release(gs);
            }
        }else if(et==XI_RawButtonPress||et==XI_RawButtonRelease){
            int btn=re->detail, pr=(et==XI_RawButtonPress)?1:0;
            if(btn==4||btn==5){
                if(pr&&g_cb_scrl){
                    PyGILState_STATE gs=PyGILState_Ensure();
                    PyObject *r=PyObject_CallFunction(g_cb_scrl,"ii",0,(btn==4)?1:-1);
                    Py_XDECREF(r); if(PyErr_Occurred())PyErr_Clear();
                    PyGILState_Release(gs);
                }
            }else if(g_cb_click){
                PyGILState_STATE gs=PyGILState_Ensure();
                PyObject *r=PyObject_CallFunction(g_cb_click,"ii",btn,pr);
                Py_XDECREF(r); if(PyErr_Occurred())PyErr_Clear();
                PyGILState_Release(gs);
            }
        }
        XFreeEventData(dpy,&xev.xcookie);
    }
    return NULL;
}

static PyObject *py_start_xi2(PyObject *self, PyObject *args)
{
    PyObject *cm,*cc,*cs; int eid=4;
    if(!PyArg_ParseTuple(args,"OOO|i",&cm,&cc,&cs,&eid)) return NULL;
    if(g_xi2_run){PyErr_SetString(PyExc_RuntimeError,"XI2 already running");return NULL;}

    g_xi2_dpy=XOpenDisplay(NULL);
    if(!g_xi2_dpy){PyErr_SetString(PyExc_RuntimeError,"Cannot open display for XI2");return NULL;}

    int ev,err;
    if(!XQueryExtension(g_xi2_dpy,"XInputExtension",&g_xi2_op,&ev,&err)){
        XCloseDisplay(g_xi2_dpy);g_xi2_dpy=NULL;
        PyErr_SetString(PyExc_RuntimeError,"XInput2 not available");return NULL;
    }
    int maj=2,min=0; XIQueryVersion(g_xi2_dpy,&maj,&min);

    Py_XDECREF(g_cb_move); g_cb_move=cm; Py_INCREF(cm);
    Py_XDECREF(g_cb_click);g_cb_click=cc;Py_INCREF(cc);
    Py_XDECREF(g_cb_scrl); g_cb_scrl=cs; Py_INCREF(cs);
    g_excl=eid; g_xi2_run=1;
    pthread_create(&g_xi2_tid,NULL,xi2_fn,NULL);
    Py_RETURN_NONE;
}

static PyObject *py_stop_xi2(PyObject *self, PyObject *args)
{
    g_xi2_run=0;
    if(g_xi2_tid){pthread_join(g_xi2_tid,NULL);g_xi2_tid=0;}
    if(g_xi2_dpy){XCloseDisplay(g_xi2_dpy);g_xi2_dpy=NULL;}
    Py_XDECREF(g_cb_move);g_cb_move=NULL;
    Py_XDECREF(g_cb_click);g_cb_click=NULL;
    Py_XDECREF(g_cb_scrl);g_cb_scrl=NULL;
    Py_RETURN_NONE;
}

/* ── uinput virtual mouse ──────────────────────────────────────────────────*/

static PyObject *py_uinput_open(PyObject *self, PyObject *args)
{
    (void)self; (void)args;
    if (g_uinput_fd >= 0) Py_RETURN_TRUE;

    int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (fd < 0) {
        PyErr_SetFromErrnoWithFilename(PyExc_PermissionError, "/dev/uinput");
        return NULL;
    }

    /* A normal relative mouse with buttons and a vertical wheel. */
    if (ioctl(fd, UI_SET_EVBIT, EV_KEY) < 0 ||
        ioctl(fd, UI_SET_EVBIT, EV_REL) < 0 ||
        ioctl(fd, UI_SET_RELBIT, REL_X) < 0 ||
        ioctl(fd, UI_SET_RELBIT, REL_Y) < 0 ||
        ioctl(fd, UI_SET_RELBIT, REL_WHEEL) < 0 ||
        ioctl(fd, UI_SET_RELBIT, REL_HWHEEL) < 0 ||
        ioctl(fd, UI_SET_KEYBIT, BTN_LEFT) < 0 ||
        ioctl(fd, UI_SET_KEYBIT, BTN_MIDDLE) < 0 ||
        ioctl(fd, UI_SET_KEYBIT, BTN_RIGHT) < 0 ||
        ioctl(fd, UI_SET_KEYBIT, BTN_SIDE) < 0 ||
        ioctl(fd, UI_SET_KEYBIT, BTN_EXTRA) < 0) {
        int saved = errno;
        close(fd);
        errno = saved;
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    struct uinput_setup setup;
    memset(&setup, 0, sizeof(setup));
    snprintf(setup.name, UINPUT_MAX_NAME_SIZE, "AOTR Virtual Mouse");
    setup.id.bustype = BUS_USB;
    setup.id.vendor = 0x1d6b;
    setup.id.product = 0x0104;
    setup.id.version = 1;
    if (ioctl(fd, UI_DEV_SETUP, &setup) < 0 || ioctl(fd, UI_DEV_CREATE) < 0) {
        int saved = errno;
        close(fd);
        errno = saved;
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    g_uinput_fd = fd;
    Py_RETURN_TRUE;
}

static PyObject *py_uinput_close(PyObject *self, PyObject *args)
{
    (void)self; (void)args;
    if (g_uinput_fd >= 0) {
        ioctl(g_uinput_fd, UI_DEV_DESTROY);
        close(g_uinput_fd);
        g_uinput_fd = -1;
    }
    Py_RETURN_NONE;
}

static PyObject *py_uinput_move(PyObject *self, PyObject *args)
{
    int dx, dy;
    if (!PyArg_ParseTuple(args, "ii", &dx, &dy)) return NULL;
    if (g_uinput_fd < 0) {
        PyErr_SetString(PyExc_RuntimeError, "uinput virtual mouse is not open");
        return NULL;
    }
    if ((dx && uinput_emit(EV_REL, REL_X, dx) < 0) ||
        (dy && uinput_emit(EV_REL, REL_Y, dy) < 0) || uinput_sync() < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *py_uinput_click(PyObject *self, PyObject *args)
{
    int button, pressed, code;
    if (!PyArg_ParseTuple(args, "ii", &button, &pressed)) return NULL;
    if (g_uinput_fd < 0) {
        PyErr_SetString(PyExc_RuntimeError, "uinput virtual mouse is not open");
        return NULL;
    }
    code = uinput_button_code(button);
    if (code < 0) {
        PyErr_SetString(PyExc_ValueError, "unsupported mouse button");
        return NULL;
    }
    if (uinput_emit(EV_KEY, (unsigned short)code, pressed ? 1 : 0) < 0 || uinput_sync() < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *py_uinput_scroll(PyObject *self, PyObject *args)
{
    int dx, dy;
    if (!PyArg_ParseTuple(args, "ii", &dx, &dy)) return NULL;
    if (g_uinput_fd < 0) {
        PyErr_SetString(PyExc_RuntimeError, "uinput virtual mouse is not open");
        return NULL;
    }
    if ((dx && uinput_emit(EV_REL, REL_HWHEEL, dx) < 0) ||
        (dy && uinput_emit(EV_REL, REL_WHEEL, dy) < 0) || uinput_sync() < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }
    Py_RETURN_NONE;
}

/* ── Module ─────────────────────────────────────────────────────────────────*/
static PyMethodDef methods[]={
    {"init_capture",  py_init_capture,  METH_VARARGS,"init_capture(w,h,x,y)"},
    {"grab_gray",     py_grab_gray,     METH_NOARGS, "grab_gray() -> uint8[h,w] numpy"},
    {"close_capture", py_close_capture, METH_NOARGS, "close_capture()"},
    {"start_xi2",     py_start_xi2,     METH_VARARGS,"start_xi2(on_move,on_click,on_scroll[,excl_id])"},
    {"stop_xi2",      py_stop_xi2,      METH_NOARGS, "stop_xi2()"},
    {"uinput_open",   py_uinput_open,   METH_NOARGS, "create uinput virtual mouse"},
    {"uinput_close",  py_uinput_close,  METH_NOARGS, "destroy uinput virtual mouse"},
    {"uinput_move",   py_uinput_move,   METH_VARARGS,"send relative motion via uinput"},
    {"uinput_click",  py_uinput_click,  METH_VARARGS,"send button event via uinput"},
    {"uinput_scroll", py_uinput_scroll, METH_VARARGS,"send scroll event via uinput"},
    {NULL,NULL,0,NULL}
};
static struct PyModuleDef moddef={PyModuleDef_HEAD_INIT,"fast_capture",NULL,-1,methods};
PyMODINIT_FUNC PyInit_fast_capture(void){import_array();return PyModule_Create(&moddef);}
