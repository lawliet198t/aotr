#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>

#include "virtual-keyboard-unstable-v1-client-protocol.h"
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

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

struct globals {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_seat *seat;
    struct zwp_virtual_keyboard_manager_v1 *keyboard_manager;
    struct zwlr_virtual_pointer_manager_v1 *pointer_manager;
    struct zwp_virtual_keyboard_v1 *keyboard;
    struct zwlr_virtual_pointer_v1 *pointer;
    struct xkb_context *xkb_context;
    struct xkb_keymap *keymap;
    struct xkb_state *xkb_state;
    bool key_down[KEY_MAX + 1];
    bool button_down[KEY_MAX + 1];
};

static volatile sig_atomic_t stop_requested = 0;
static struct globals g;

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
    int result = read_exact(fd, header, sizeof(header));
    if (result != 1 || memcmp(header, "AOTR", 4) != 0 || le32(header + 4) != INPUT_VERSION) {
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

static void registry_global(void *data, struct wl_registry *registry,
        uint32_t name, const char *interface, uint32_t version) {
    (void)data;
    if (strcmp(interface, wl_seat_interface.name) == 0 && g.seat == NULL) {
        g.seat = wl_registry_bind(registry, name, &wl_seat_interface,
                version < 7 ? version : 7);
    } else if (strcmp(interface, zwp_virtual_keyboard_manager_v1_interface.name) == 0 &&
            g.keyboard_manager == NULL) {
        g.keyboard_manager = wl_registry_bind(registry, name,
                &zwp_virtual_keyboard_manager_v1_interface, 1);
    } else if (strcmp(interface, zwlr_virtual_pointer_manager_v1_interface.name) == 0 &&
            g.pointer_manager == NULL) {
        g.pointer_manager = wl_registry_bind(registry, name,
                &zwlr_virtual_pointer_manager_v1_interface, version < 2 ? version : 2);
    }
}

static void registry_global_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data;
    (void)registry;
    (void)name;
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

static bool flush_display(void) {
    while (wl_display_flush(g.display) < 0) {
        if (errno == EINTR) continue;
        if (errno != EAGAIN) {
            fprintf(stderr, "Wayland flush failed: %s\n", strerror(errno));
            return false;
        }
        struct pollfd pfd = { .fd = wl_display_get_fd(g.display), .events = POLLOUT };
        if (poll(&pfd, 1, 1000) < 0 && errno != EINTR) {
            fprintf(stderr, "Waiting for Wayland socket failed: %s\n", strerror(errno));
            return false;
        }
    }
    return true;
}

static bool send_keymap(void) {
    g.xkb_context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    if (g.xkb_context == NULL) {
        fprintf(stderr, "Could not create the XKB context.\n");
        return false;
    }
    g.keymap = xkb_keymap_new_from_names(g.xkb_context, NULL, XKB_KEYMAP_COMPILE_NO_FLAGS);
    if (g.keymap == NULL) {
        fprintf(stderr, "Could not build a keymap from the current XKB environment.\n");
        return false;
    }
    g.xkb_state = xkb_state_new(g.keymap);
    if (g.xkb_state == NULL) {
        fprintf(stderr, "Could not create the XKB state.\n");
        return false;
    }
    char *text = xkb_keymap_get_as_string(g.keymap, XKB_KEYMAP_FORMAT_TEXT_V1);
    if (text == NULL) {
        fprintf(stderr, "Could not serialize the XKB keymap.\n");
        return false;
    }
    size_t size = strlen(text) + 1;
    int fd = memfd_create("aotr-virtual-keymap", MFD_CLOEXEC | MFD_ALLOW_SEALING);
    if (fd < 0) {
        perror("memfd_create for keyboard keymap");
        free(text);
        return false;
    }
    size_t offset = 0;
    while (offset < size) {
        ssize_t written = write(fd, text + offset, size - offset);
        if (written < 0) {
            if (errno == EINTR) continue;
            perror("writing keyboard keymap");
            close(fd);
            free(text);
            return false;
        }
        offset += (size_t)written;
    }
    free(text);
    (void)fcntl(fd, F_ADD_SEALS, F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL);
    zwp_virtual_keyboard_v1_keymap(g.keyboard, WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1, fd,
            (uint32_t)size);
    bool ok = flush_display();
    close(fd);
    if (!ok || wl_display_roundtrip(g.display) < 0) {
        fprintf(stderr, "Compositor did not accept the virtual keyboard keymap.\n");
        return false;
    }
    return true;
}

static bool connect_wayland(void) {
    g.display = wl_display_connect(NULL);
    if (g.display == NULL) {
        fprintf(stderr, "Could not connect to the private Wayland display. Check XDG_RUNTIME_DIR and WAYLAND_DISPLAY.\n");
        return false;
    }
    g.registry = wl_display_get_registry(g.display);
    if (g.registry == NULL || wl_registry_add_listener(g.registry, &registry_listener, NULL) < 0 ||
            wl_display_roundtrip(g.display) < 0) {
        fprintf(stderr, "Could not enumerate Wayland input protocols.\n");
        return false;
    }
    if (g.seat == NULL || g.keyboard_manager == NULL || g.pointer_manager == NULL) {
        fprintf(stderr, "Sway did not expose the virtual keyboard and pointer protocols.\n");
        return false;
    }
    g.keyboard = zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(
            g.keyboard_manager, g.seat);
    g.pointer = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(
            g.pointer_manager, g.seat);
    if (g.keyboard == NULL || g.pointer == NULL || !send_keymap()) {
        fprintf(stderr, "Could not create isolated virtual input devices.\n");
        return false;
    }
    return true;
}

static uint32_t wayland_time_ms(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint32_t)((uint64_t)now.tv_sec * 1000u + (uint64_t)now.tv_nsec / 1000000u);
}

static void send_modifiers(void) {
    uint32_t depressed = xkb_state_serialize_mods(g.xkb_state, XKB_STATE_MODS_DEPRESSED);
    uint32_t latched = xkb_state_serialize_mods(g.xkb_state, XKB_STATE_MODS_LATCHED);
    uint32_t locked = xkb_state_serialize_mods(g.xkb_state, XKB_STATE_MODS_LOCKED);
    uint32_t group = xkb_state_serialize_layout(g.xkb_state, XKB_STATE_LAYOUT_EFFECTIVE);
    zwp_virtual_keyboard_v1_modifiers(g.keyboard, depressed, latched, locked, group);
}

static bool client_disconnected(int fd);

static void keyboard_event(uint16_t code, int32_t value) {
    if (code > KEY_MAX || value < 0 || value > 2) return;
    uint32_t state = value == 0 ? WL_KEYBOARD_KEY_STATE_RELEASED : WL_KEYBOARD_KEY_STATE_PRESSED;
    zwp_virtual_keyboard_v1_key(g.keyboard, wayland_time_ms(), code, state);
    if (value == 0) {
        g.key_down[code] = false;
        xkb_state_update_key(g.xkb_state, (xkb_keycode_t)code + 8, XKB_KEY_UP);
        send_modifiers();
    } else if (value == 1) {
        g.key_down[code] = true;
        xkb_state_update_key(g.xkb_state, (xkb_keycode_t)code + 8, XKB_KEY_DOWN);
        send_modifiers();
    }
    /* value 2 is an already-held kernel repeat; forward another press without
       changing our held-key modifier state. */
}

static void pointer_event(uint16_t code, int32_t value) {
    if (code > KEY_MAX || value < 0 || value > 2) return;
    uint32_t state = value == 0 ? WL_POINTER_BUTTON_STATE_RELEASED : WL_POINTER_BUTTON_STATE_PRESSED;
    zwlr_virtual_pointer_v1_button(g.pointer, wayland_time_ms(), code, state);
    g.button_down[code] = value != 0;
    zwlr_virtual_pointer_v1_frame(g.pointer);
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
        if (result == 0) continue;
        errno = result;
        perror("clock_nanosleep");
        return false;
    }
    return false;
}

static void release_inputs(void) {
    if (g.keyboard != NULL && g.xkb_state != NULL) {
        for (uint16_t code = 0; code <= KEY_MAX; ++code) {
            if (!g.key_down[code]) continue;
            zwp_virtual_keyboard_v1_key(g.keyboard, wayland_time_ms(), code,
                    WL_KEYBOARD_KEY_STATE_RELEASED);
            xkb_state_update_key(g.xkb_state, (xkb_keycode_t)code + 8, XKB_KEY_UP);
            g.key_down[code] = false;
            send_modifiers();
        }
    }
    if (g.pointer != NULL) {
        for (uint16_t code = BTN_MOUSE; code <= KEY_MAX; ++code) {
            if (!g.button_down[code]) continue;
            zwlr_virtual_pointer_v1_button(g.pointer, wayland_time_ms(), code,
                    WL_POINTER_BUTTON_STATE_RELEASED);
            g.button_down[code] = false;
        }
        zwlr_virtual_pointer_v1_frame(g.pointer);
    }
    if (g.display != NULL) (void)flush_display();
}

static void disconnect_wayland(void) {
    if (g.keyboard != NULL) zwp_virtual_keyboard_v1_destroy(g.keyboard);
    if (g.pointer != NULL) zwlr_virtual_pointer_v1_destroy(g.pointer);
    if (g.pointer_manager != NULL) zwlr_virtual_pointer_manager_v1_destroy(g.pointer_manager);
    if (g.seat != NULL) wl_seat_destroy(g.seat);
    if (g.registry != NULL) wl_registry_destroy(g.registry);
    if (g.display != NULL) wl_display_disconnect(g.display);
    if (g.xkb_state != NULL) xkb_state_unref(g.xkb_state);
    if (g.keymap != NULL) xkb_keymap_unref(g.keymap);
    if (g.xkb_context != NULL) xkb_context_unref(g.xkb_context);
    memset(&g, 0, sizeof(g));
}

static bool handle_frame(const struct replay_frame *frame) {
    bool pointer_changed = false;
    int32_t dx = 0, dy = 0;
    for (uint16_t i = 0; i < frame->count; ++i) {
        const struct raw_event *event = &frame->events[i];
        if (event->type == EV_KEY) {
            if (frame->kind == FRAME_KEYBOARD) {
                if (event->code == KEY_ESC && event->value == 1) {
                    fprintf(stderr, "Refusing a replay containing an Escape key press. Remove it or edit the recording first.\n");
                    return false;
                }
                keyboard_event((uint16_t)event->code, event->value);
            } else if (frame->kind == FRAME_POINTER && event->code >= BTN_MOUSE) {
                pointer_event((uint16_t)event->code, event->value);
                pointer_changed = true;
            }
        } else if (event->type == EV_REL && frame->kind == FRAME_POINTER) {
            switch (event->code) {
            case REL_X: dx += event->value; break;
            case REL_Y: dy += event->value; break;
            case REL_WHEEL:
                zwlr_virtual_pointer_v1_axis_source(g.pointer, WL_POINTER_AXIS_SOURCE_WHEEL);
                zwlr_virtual_pointer_v1_axis(g.pointer, wayland_time_ms(),
                        WL_POINTER_AXIS_VERTICAL_SCROLL,
                        wl_fixed_from_double(-(double)event->value * 15.0));
                zwlr_virtual_pointer_v1_axis_discrete(g.pointer, wayland_time_ms(),
                        WL_POINTER_AXIS_VERTICAL_SCROLL,
                        wl_fixed_from_double(-(double)event->value * 15.0), -event->value);
                pointer_changed = true;
                break;
            case REL_HWHEEL:
                zwlr_virtual_pointer_v1_axis_source(g.pointer, WL_POINTER_AXIS_SOURCE_WHEEL);
                zwlr_virtual_pointer_v1_axis(g.pointer, wayland_time_ms(),
                        WL_POINTER_AXIS_HORIZONTAL_SCROLL,
                        wl_fixed_from_double((double)event->value * 15.0));
                zwlr_virtual_pointer_v1_axis_discrete(g.pointer, wayland_time_ms(),
                        WL_POINTER_AXIS_HORIZONTAL_SCROLL,
                        wl_fixed_from_double((double)event->value * 15.0), event->value);
                pointer_changed = true;
                break;
            case REL_WHEEL_HI_RES:
                /* The sender filters coarse wheel events when hi-res data exists. */
                zwlr_virtual_pointer_v1_axis_source(g.pointer, WL_POINTER_AXIS_SOURCE_WHEEL);
                zwlr_virtual_pointer_v1_axis(g.pointer, wayland_time_ms(),
                        WL_POINTER_AXIS_VERTICAL_SCROLL,
                        wl_fixed_from_double(-(double)event->value * 15.0 / 120.0));
                pointer_changed = true;
                break;
            case REL_HWHEEL_HI_RES:
                zwlr_virtual_pointer_v1_axis_source(g.pointer, WL_POINTER_AXIS_SOURCE_WHEEL);
                zwlr_virtual_pointer_v1_axis(g.pointer, wayland_time_ms(),
                        WL_POINTER_AXIS_HORIZONTAL_SCROLL,
                        wl_fixed_from_double((double)event->value * 15.0 / 120.0));
                pointer_changed = true;
                break;
            default: break;
            }
        }
    }
    if (dx != 0 || dy != 0) {
        zwlr_virtual_pointer_v1_motion(g.pointer, wayland_time_ms(),
                wl_fixed_from_int(dx), wl_fixed_from_int(dy));
        pointer_changed = true;
    }
    if (pointer_changed) zwlr_virtual_pointer_v1_frame(g.pointer);
    return flush_display();
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
    uint64_t start_ns = (uint64_t)now.tv_sec * 1000000000u +
        (uint64_t)now.tv_nsec + 200000000u;

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
        fprintf(stderr, "[headless-replay] Completed %u/%u frames; virtual devices remain armed.\n",
                completed, frame_count);
        (void)send_message(fd, "DONE\n");
    } else {
        (void)send_message(fd, "ERROR replay rejected\n");
    }
    free(frames);
}

int main(int argc, char **argv) {
    if (argc != 3 || strcmp(argv[1], "--server") != 0) {
        fprintf(stderr, "Usage: %s --server SOCKET_PATH\n", argv[0]);
        return 2;
    }
    const char *socket_path = argv[2];
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);

    if (!connect_wayland()) {
        disconnect_wayland();
        return 2;
    }
    int server = create_listener(socket_path);
    if (server < 0) {
        disconnect_wayland();
        return 2;
    }
    fprintf(stderr, "[headless-replay] Persistent private Wayland input devices armed at %s.\n", socket_path);

    while (!stop_requested) {
        struct pollfd watched[2] = {
            { .fd = server, .events = POLLIN },
            { .fd = wl_display_get_fd(g.display), .events = 0 },
        };
        int ready = poll(watched, 2, -1);
        if (ready < 0) {
            if (errno == EINTR && !stop_requested) continue;
            if (!stop_requested) perror("waiting for replay or Wayland events");
            break;
        }
        if (watched[1].revents & (POLLERR | POLLHUP | POLLNVAL)) {
            fprintf(stderr, "[headless-replay] Wayland compositor closed its connection.\n");
            break;
        }
        if (!(watched[0].revents & POLLIN)) continue;
        int client = accept4(server, NULL, NULL, SOCK_CLOEXEC);
        if (client < 0) {
            if (errno == EINTR && !stop_requested) continue;
            if (!stop_requested) perror("accepting replay client");
            break;
        }
        replay_client(client);
        close(client);
        if (wl_display_get_error(g.display) != 0) {
            fprintf(stderr, "[headless-replay] Wayland compositor connection was lost.\n");
            break;
        }
    }
    close(server);
    unlink(socket_path);
    release_inputs();
    disconnect_wayland();
    return stop_requested ? 0 : 1;
}
