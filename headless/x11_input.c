#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <X11/Xlib.h>
#include <X11/extensions/XTest.h>

#define INPUT_VERSION 1u
#define MAX_FRAMES 200000u
#define MAX_EVENTS_PER_FRAME 64u

enum frame_kind { FRAME_KEYBOARD = 1, FRAME_POINTER = 2 };

struct raw_event {
    int32_t type;
    int32_t code;
    int32_t value;
};

struct replay_frame {
    uint64_t at_usec;
    uint8_t kind;
    uint16_t count;
    struct raw_event events[MAX_EVENTS_PER_FRAME];
};

static Display *display;
static bool key_down[KEY_MAX + 1];
static bool button_down[256];
static int32_t vertical_wheel;
static int32_t horizontal_wheel;
static volatile sig_atomic_t stop_requested;

static void on_signal(int signal_number) {
    (void)signal_number;
    stop_requested = 1;
}

static int read_exact(int fd, void *buffer, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        ssize_t got = read(fd, (char *)buffer + offset, size - offset);
        if (got == 0) return offset == 0 ? 0 : -1;
        if (got < 0) {
            if (errno == EINTR && !stop_requested) continue;
            return -1;
        }
        offset += (size_t)got;
    }
    return 1;
}

static uint16_t le16(const unsigned char *p) {
    return (uint16_t)p[0] | (uint16_t)((uint16_t)p[1] << 8);
}

static uint32_t le32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
        ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint64_t le64(const unsigned char *p) {
    return (uint64_t)le32(p) | ((uint64_t)le32(p + 4) << 32);
}

static bool load_frames(int fd, struct replay_frame **out_frames, uint32_t *out_count) {
    unsigned char header[12];
    if (read_exact(fd, header, sizeof(header)) != 1 ||
            memcmp(header, "AOTR", 4) != 0 || le32(header + 4) != INPUT_VERSION) {
        fprintf(stderr, "Invalid replay stream header.\n");
        return false;
    }
    uint32_t count = le32(header + 8);
    if (count == 0 || count > MAX_FRAMES) {
        fprintf(stderr, "Replay frame count is invalid: %u\n", count);
        return false;
    }
    struct replay_frame *frames = calloc(count, sizeof(*frames));
    if (frames == NULL) {
        perror("allocating replay frames");
        return false;
    }
    for (uint32_t i = 0; i < count; ++i) {
        unsigned char frame_header[12];
        if (read_exact(fd, frame_header, sizeof(frame_header)) != 1) {
            fprintf(stderr, "Replay stream ended at frame %u.\n", i);
            free(frames);
            return false;
        }
        frames[i].at_usec = le64(frame_header);
        frames[i].kind = frame_header[8];
        frames[i].count = le16(frame_header + 10);
        if ((frames[i].kind != FRAME_KEYBOARD && frames[i].kind != FRAME_POINTER) ||
                frames[i].count > MAX_EVENTS_PER_FRAME ||
                (i > 0 && frames[i].at_usec < frames[i - 1].at_usec)) {
            fprintf(stderr, "Invalid frame metadata at frame %u.\n", i);
            free(frames);
            return false;
        }
        for (uint16_t j = 0; j < frames[i].count; ++j) {
            unsigned char event_data[12];
            if (read_exact(fd, event_data, sizeof(event_data)) != 1) {
                fprintf(stderr, "Replay stream ended inside frame %u.\n", i);
                free(frames);
                return false;
            }
            frames[i].events[j].type = (int32_t)le32(event_data);
            frames[i].events[j].code = (int32_t)le32(event_data + 4);
            frames[i].events[j].value = (int32_t)le32(event_data + 8);
        }
    }
    *out_frames = frames;
    *out_count = count;
    return true;
}

static bool fake_key(uint16_t evdev_code, int32_t value) {
    unsigned int xcode = (unsigned int)evdev_code + 8u;
    if (evdev_code > KEY_MAX || xcode > 255u || value < 0 || value > 2) return true;
    Bool press = value == 0 ? False : True;
    if (!XTestFakeKeyEvent(display, xcode, press, CurrentTime)) return false;
    key_down[evdev_code] = value != 0;
    return true;
}

static unsigned int x_button_for_evdev(int32_t code) {
    switch (code) {
    case BTN_LEFT: return 1;
    case BTN_MIDDLE: return 2;
    case BTN_RIGHT: return 3;
    case BTN_SIDE: return 8;
    case BTN_EXTRA: return 9;
    case BTN_FORWARD: return 8;
    case BTN_BACK: return 9;
    case BTN_TASK: return 10;
    default: return 0;
    }
}

static bool fake_button(unsigned int button, int32_t value) {
    if (button == 0 || button >= 256 || value < 0 || value > 2) return true;
    Bool press = value == 0 ? False : True;
    if (!XTestFakeButtonEvent(display, button, press, CurrentTime)) return false;
    button_down[button] = value != 0;
    return true;
}

static bool fake_wheel(bool horizontal, int32_t value, bool high_res) {
    int32_t *accumulator = horizontal ? &horizontal_wheel : &vertical_wheel;
    int64_t scaled = high_res ? value : (int64_t)value * 120;
    int64_t total = (int64_t)*accumulator + scaled;
    if (total > INT32_MAX) total = INT32_MAX;
    if (total < INT32_MIN) total = INT32_MIN;
    *accumulator = (int32_t)total;
    while (*accumulator >= 120) {
        unsigned int button = horizontal ? 7u : 4u;
        if (!fake_button(button, 1) || !fake_button(button, 0)) return false;
        *accumulator -= 120;
    }
    while (*accumulator <= -120) {
        unsigned int button = horizontal ? 6u : 5u;
        if (!fake_button(button, 1) || !fake_button(button, 0)) return false;
        *accumulator += 120;
    }
    return true;
}

static bool handle_frame(const struct replay_frame *frame) {
    int64_t dx = 0, dy = 0;
    bool pointer_changed = false;
    for (uint16_t i = 0; i < frame->count; ++i) {
        const struct raw_event *event = &frame->events[i];
        if (event->type == EV_KEY) {
            if (frame->kind == FRAME_KEYBOARD) {
                if (event->code == KEY_ESC && event->value == 1) {
                    fprintf(stderr, "Refusing a replay containing an Escape key press.\n");
                    return false;
                }
                if (event->code >= 0 && event->code <= KEY_MAX &&
                        !fake_key((uint16_t)event->code, event->value)) return false;
            } else if (frame->kind == FRAME_POINTER && event->code >= BTN_MOUSE) {
                unsigned int button = x_button_for_evdev(event->code);
                if (button != 0 && !fake_button(button, event->value)) return false;
                pointer_changed = true;
            }
        } else if (event->type == EV_REL && frame->kind == FRAME_POINTER) {
            switch (event->code) {
            case REL_X: dx += event->value; break;
            case REL_Y: dy += event->value; break;
            case REL_WHEEL:
                if (!fake_wheel(false, event->value, false)) return false;
                pointer_changed = true;
                break;
            case REL_HWHEEL:
                if (!fake_wheel(true, event->value, false)) return false;
                pointer_changed = true;
                break;
            case REL_WHEEL_HI_RES:
                if (!fake_wheel(false, event->value, true)) return false;
                pointer_changed = true;
                break;
            case REL_HWHEEL_HI_RES:
                if (!fake_wheel(true, event->value, true)) return false;
                pointer_changed = true;
                break;
            default: break;
            }
        }
    }
    if (dx != 0 || dy != 0) {
        if (dx > INT_MAX) dx = INT_MAX;
        if (dx < INT_MIN) dx = INT_MIN;
        if (dy > INT_MAX) dy = INT_MAX;
        if (dy < INT_MIN) dy = INT_MIN;
        if (!XTestFakeRelativeMotionEvent(display, (int)dx, (int)dy, CurrentTime)) return false;
        pointer_changed = true;
    }
    (void)pointer_changed;
    XFlush(display);
    return true;
}

static void release_inputs(void) {
    for (uint16_t code = 0; code <= KEY_MAX; ++code) {
        if (!key_down[code]) continue;
        (void)XTestFakeKeyEvent(display, (unsigned int)code + 8u, False, CurrentTime);
        key_down[code] = false;
    }
    for (unsigned int button = 1; button < 256; ++button) {
        if (!button_down[button]) continue;
        (void)XTestFakeButtonEvent(display, button, False, CurrentTime);
        button_down[button] = false;
    }
    if (display != NULL) XFlush(display);
    vertical_wheel = horizontal_wheel = 0;
}

static bool client_disconnected(int fd) {
    unsigned char byte;
    ssize_t result = recv(fd, &byte, 1, MSG_PEEK | MSG_DONTWAIT);
    return result == 0 || (result < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR);
}

static bool send_message(int fd, const char *message) {
    size_t length = strlen(message), offset = 0;
    while (offset < length) {
        ssize_t sent = send(fd, message + offset, length - offset, MSG_NOSIGNAL);
        if (sent < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        offset += (size_t)sent;
    }
    return true;
}

static bool schedule_until(uint64_t start_ns, uint64_t at_usec, int client_fd) {
    uint64_t target_ns = start_ns + at_usec * 1000u;
    while (!stop_requested) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        uint64_t now_ns = (uint64_t)now.tv_sec * 1000000000u + (uint64_t)now.tv_nsec;
        if (now_ns >= target_ns) return true;
        uint64_t wake_ns = now_ns + 50000000u;
        if (wake_ns > target_ns) wake_ns = target_ns;
        struct timespec wake = {
            .tv_sec = (time_t)(wake_ns / 1000000000u),
            .tv_nsec = (long)(wake_ns % 1000000000u),
        };
        int result = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &wake, NULL);
        if (result == 0 && client_disconnected(client_fd)) return false;
        if (result == EINTR) continue;
        if (result != 0) {
            errno = result;
            perror("clock_nanosleep");
            return false;
        }
    }
    return false;
}

static void replay_client(int fd) {
    struct replay_frame *frames = NULL;
    uint32_t frame_count = 0;
    if (!load_frames(fd, &frames, &frame_count)) {
        (void)send_message(fd, "ERROR invalid replay stream\n");
        return;
    }
    (void)send_message(fd, "READY\n");
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint64_t start_ns = (uint64_t)now.tv_sec * 1000000000u + (uint64_t)now.tv_nsec + 20000000u;

    bool ok = true, cancelled = false;
    uint32_t completed = 0;
    for (; completed < frame_count && !stop_requested; ++completed) {
        if (client_disconnected(fd)) { cancelled = true; break; }
        if (!schedule_until(start_ns, frames[completed].at_usec, fd)) {
            if (client_disconnected(fd)) cancelled = true;
            else if (!stop_requested) ok = false;
            break;
        }
        if (client_disconnected(fd)) { cancelled = true; break; }
        if (!handle_frame(&frames[completed])) { ok = false; break; }
    }
    release_inputs();
    if (cancelled) {
        fprintf(stderr, "[headless-replay] Client disconnected; cancelled and released inputs.\n");
        (void)send_message(fd, "CANCELLED\n");
    } else if (stop_requested) {
        (void)send_message(fd, "CANCELLED\n");
    } else if (ok) {
        fprintf(stderr, "[headless-replay] XTest replay completed %u/%u frames.\n", completed, frame_count);
        (void)send_message(fd, "DONE\n");
    } else {
        fprintf(stderr, "[headless-replay] XTest rejected an event at frame %u/%u.\n", completed, frame_count);
        (void)send_message(fd, "ERROR replay rejected\n");
    }
    free(frames);
}

static int create_listener(const char *path) {
    if (strlen(path) >= sizeof(((struct sockaddr_un *)0)->sun_path)) {
        fprintf(stderr, "Input socket path is too long.\n");
        return -1;
    }
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        perror("creating input socket");
        return -1;
    }
    struct sockaddr_un address = { .sun_family = AF_UNIX };
    memcpy(address.sun_path, path, strlen(path) + 1);
    unlink(path);
    if (bind(fd, (struct sockaddr *)&address, sizeof(address)) < 0 ||
            chmod(path, 0600) < 0 || listen(fd, 4) < 0) {
        perror("preparing input socket");
        close(fd);
        unlink(path);
        return -1;
    }
    return fd;
}

int main(int argc, char **argv) {
    if (argc != 3 || strcmp(argv[1], "--server") != 0) {
        fprintf(stderr, "Usage: %s --server SOCKET_PATH\n", argv[0]);
        return 2;
    }
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);

    display = XOpenDisplay(NULL);
    if (display == NULL) {
        fprintf(stderr, "Could not open private XWayland display '%s'. Check DISPLAY and XAUTHORITY.\n",
                XDisplayName(NULL));
        return 2;
    }
    int event_base, error_base, major, minor;
    if (!XTestQueryExtension(display, &event_base, &error_base, &major, &minor)) {
        fprintf(stderr, "Private XWayland does not expose the XTest extension.\n");
        XCloseDisplay(display);
        return 2;
    }
    int server = create_listener(argv[2]);
    if (server < 0) {
        XCloseDisplay(display);
        return 2;
    }
    fprintf(stderr, "[headless-replay] Persistent XTest input armed on %s (XTest %d.%d).\n",
            DisplayString(display), major, minor);

    while (!stop_requested) {
        struct pollfd watched = { .fd = server, .events = POLLIN };
        int ready = poll(&watched, 1, -1);
        if (ready < 0) {
            if (errno == EINTR && !stop_requested) continue;
            if (!stop_requested) perror("waiting for replay client");
            break;
        }
        if (!(watched.revents & POLLIN)) continue;
        int client = accept4(server, NULL, NULL, SOCK_CLOEXEC);
        if (client < 0) {
            if (errno == EINTR && !stop_requested) continue;
            if (!stop_requested) perror("accepting replay client");
            break;
        }
        replay_client(client);
        close(client);
    }
    release_inputs();
    close(server);
    unlink(argv[2]);
    XCloseDisplay(display);
    return stop_requested ? 0 : 1;
}
