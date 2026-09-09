#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent

def replace_once(rel, old, new):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{rel}: expected exactly 1 match, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"patched {rel}")

def replace_all(rel, old, new, expected=None):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if expected is not None and count != expected:
        raise RuntimeError(f"{rel}: expected {expected} matches, found {count}")
    if count == 0:
        raise RuntimeError(f"{rel}: no matches for requested replacement")
    p.write_text(text.replace(old, new), encoding="utf-8")
    print(f"patched {rel} ({count} replacements)")

UGREEN_H = r'''#pragma once

#include <SDL.h>
#include <stdbool.h>

typedef struct app_t app_t;
typedef struct session_t session_t;

/*
 * Raw input bridge for the user's UGREEN 2.4 GHz receivers.
 * USB VID:PID = 2b89:0043.
 *
 * All matching /dev/input/event* nodes are EVIOCGRAB'ed while Aurora is
 * foregrounded. The nodes are drained immediately, but mouse/keyboard events
 * are only forwarded to Sunshine while a stream is accepting input.
 */
typedef struct ugreen_input_t {
    app_t *app;
    SDL_Thread *thread;
    SDL_mutex *session_lock;
    SDL_atomic_t running;
    SDL_atomic_t active;
    session_t *session;
    unsigned int session_generation;
} ugreen_input_t;

int ugreen_input_init(ugreen_input_t *input, app_t *app);
void ugreen_input_deinit(ugreen_input_t *input);
void ugreen_input_set_active(ugreen_input_t *input, bool active);
void ugreen_input_set_session(ugreen_input_t *input, session_t *session);
'''

UGREEN_C = r'''#include "config.h"
#include "ugreen_input.h"

#if TARGET_WEBOS

#include <Limelight.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/select.h>
#include <unistd.h>

#include "logging.h"
#include "stream/input/vk.h"
#include "stream/session.h"

#define UGREEN_VENDOR_ID  0x2b89
#define UGREEN_PRODUCT_ID 0x0043
#define UGREEN_MAX_DEVICES 24

#define UGREEN_BITS_PER_LONG (sizeof(unsigned long) * 8)
#define UGREEN_NBITS(x) ((((x) - 1) / UGREEN_BITS_PER_LONG) + 1)

typedef enum {
    UGREEN_DEV_OTHER = 0,
    UGREEN_DEV_MOUSE,
    UGREEN_DEV_KEYBOARD,
    UGREEN_DEV_AUX_KEYBOARD,
} ugreen_dev_kind_t;

typedef struct {
    int fd;
    char path[64];
    char name[128];
    ugreen_dev_kind_t kind;
    int dx;
    int dy;
} ugreen_dev_t;

typedef struct {
    bool sent_keys[KEY_MAX + 1];
    bool sent_mouse[6];
    char modifiers;
    unsigned int session_generation;
    bool was_accepting;
} ugreen_runtime_state_t;

static bool bit_is_set(int bit, const unsigned long *bits) {
    return (bits[bit / UGREEN_BITS_PER_LONG] &
            (1UL << (bit % UGREEN_BITS_PER_LONG))) != 0;
}

static bool session_can_send_locked(ugreen_input_t *input, bool require_accepting) {
    if (input->session == NULL) {
        return false;
    }

    stream_input_t *stream_input = session_get_input(input->session);
    if (stream_input == NULL || stream_input->view_only) {
        return false;
    }

    if (require_accepting) {
        return session_accepting_input(input->session);
    }
    return session_has_input(input->session);
}

static bool ugreen_is_accepting(ugreen_input_t *input, unsigned int *generation) {
    bool result;
    SDL_LockMutex(input->session_lock);
    if (generation != NULL) {
        *generation = input->session_generation;
    }
    result = session_can_send_locked(input, true);
    SDL_UnlockMutex(input->session_lock);
    return result;
}

static void send_mouse_move(ugreen_input_t *input, int dx, int dy) {
    if (dx == 0 && dy == 0) return;

    if (dx > 32767) dx = 32767;
    if (dx < -32768) dx = -32768;
    if (dy > 32767) dy = 32767;
    if (dy < -32768) dy = -32768;

    SDL_LockMutex(input->session_lock);
    if (session_can_send_locked(input, true)) {
        LiSendMouseMoveEvent((short) dx, (short) dy);
    }
    SDL_UnlockMutex(input->session_lock);
}

static void send_mouse_scroll(ugreen_input_t *input, int value, bool horizontal) {
    if (value == 0) return;
    if (value > 127) value = 127;
    if (value < -127) value = -127;

    SDL_LockMutex(input->session_lock);
    if (session_can_send_locked(input, true)) {
        if (horizontal) LiSendHScrollEvent((signed char) value);
        else LiSendScrollEvent((signed char) value);
    }
    SDL_UnlockMutex(input->session_lock);
}

static int linux_button_to_moonlight(unsigned short code) {
    switch (code) {
        case BTN_LEFT: return BUTTON_LEFT;
        case BTN_MIDDLE: return BUTTON_MIDDLE;
        case BTN_RIGHT: return BUTTON_RIGHT;
        case BTN_SIDE:
        case BTN_BACK: return BUTTON_X1;
        case BTN_EXTRA:
        case BTN_FORWARD: return BUTTON_X2;
        default: return 0;
    }
}

static bool send_mouse_button(ugreen_input_t *input, int button, bool pressed,
                              bool require_accepting) {
    bool sent = false;
    SDL_LockMutex(input->session_lock);
    if (session_can_send_locked(input, require_accepting)) {
        LiSendMouseButtonEvent(pressed ? BUTTON_ACTION_PRESS : BUTTON_ACTION_RELEASE, button);
        sent = true;
    }
    SDL_UnlockMutex(input->session_lock);
    return sent;
}

static short linux_key_to_vk(unsigned short code) {
    if (code >= KEY_F1 && code <= KEY_F10) {
        return (short) (VK_F1 + (code - KEY_F1));
    }

    switch (code) {
        case KEY_ESC: return VK_ESCAPE;
        case KEY_1: return VK_1; case KEY_2: return VK_2; case KEY_3: return VK_3;
        case KEY_4: return VK_4; case KEY_5: return VK_5; case KEY_6: return VK_6;
        case KEY_7: return VK_7; case KEY_8: return VK_8; case KEY_9: return VK_9;
        case KEY_0: return VK_0;
        case KEY_MINUS: return VK_OEM_MINUS;
        case KEY_EQUAL: return VK_OEM_PLUS;
        case KEY_BACKSPACE: return VK_BACK;
        case KEY_TAB: return VK_TAB;

        case KEY_Q: return VK_Q; case KEY_W: return VK_W; case KEY_E: return VK_E;
        case KEY_R: return VK_R; case KEY_T: return VK_T; case KEY_Y: return VK_Y;
        case KEY_U: return VK_U; case KEY_I: return VK_I; case KEY_O: return VK_O;
        case KEY_P: return VK_P;

        case KEY_LEFTBRACE: return VK_OEM_4;
        case KEY_RIGHTBRACE: return VK_OEM_6;
        case KEY_ENTER: return VK_RETURN;
        case KEY_LEFTCTRL: return VK_LCONTROL;

        case KEY_A: return VK_A; case KEY_S: return VK_S; case KEY_D: return VK_D;
        case KEY_F: return VK_F; case KEY_G: return VK_G; case KEY_H: return VK_H;
        case KEY_J: return VK_J; case KEY_K: return VK_K; case KEY_L: return VK_L;

        case KEY_SEMICOLON: return VK_OEM_1;
        case KEY_APOSTROPHE: return VK_OEM_7;
        case KEY_GRAVE: return VK_OEM_3;
        case KEY_LEFTSHIFT: return VK_LSHIFT;
        case KEY_BACKSLASH: return VK_OEM_5;

        case KEY_Z: return VK_Z; case KEY_X: return VK_X; case KEY_C: return VK_C;
        case KEY_V: return VK_V; case KEY_B: return VK_B; case KEY_N: return VK_N;
        case KEY_M: return VK_M;

        case KEY_COMMA: return VK_OEM_COMMA;
        case KEY_DOT: return VK_OEM_PERIOD;
        case KEY_SLASH: return VK_OEM_2;
        case KEY_RIGHTSHIFT: return VK_RSHIFT;
        case KEY_KPASTERISK: return VK_MULTIPLY;
        case KEY_LEFTALT: return VK_LMENU;
        case KEY_SPACE: return VK_SPACE;
        case KEY_CAPSLOCK: return VK_CAPITAL;

        case KEY_F11: return VK_F11;
        case KEY_F12: return VK_F12;
        case KEY_NUMLOCK: return VK_NUMLOCK;
        case KEY_SCROLLLOCK: return VK_SCROLL;

        case KEY_KP7: return VK_NUMPAD7; case KEY_KP8: return VK_NUMPAD8;
        case KEY_KP9: return VK_NUMPAD9; case KEY_KPMINUS: return VK_SUBTRACT;
        case KEY_KP4: return VK_NUMPAD4; case KEY_KP5: return VK_NUMPAD5;
        case KEY_KP6: return VK_NUMPAD6; case KEY_KPPLUS: return VK_ADD;
        case KEY_KP1: return VK_NUMPAD1; case KEY_KP2: return VK_NUMPAD2;
        case KEY_KP3: return VK_NUMPAD3; case KEY_KP0: return VK_NUMPAD0;
        case KEY_KPDOT: return VK_DECIMAL;
        case KEY_KPENTER: return VK_RETURN;
        case KEY_RIGHTCTRL: return VK_RCONTROL;
        case KEY_KPSLASH: return VK_DIVIDE;
        case KEY_SYSRQ: return VK_SNAPSHOT;
        case KEY_RIGHTALT: return VK_RMENU;

        case KEY_HOME: return VK_HOME; case KEY_UP: return VK_UP;
        case KEY_PAGEUP: return VK_PRIOR; case KEY_LEFT: return VK_LEFT;
        case KEY_RIGHT: return VK_RIGHT; case KEY_END: return VK_END;
        case KEY_DOWN: return VK_DOWN; case KEY_PAGEDOWN: return VK_NEXT;
        case KEY_INSERT: return VK_INSERT; case KEY_DELETE: return VK_DELETE;
        case KEY_PAUSE: return VK_PAUSE;

        case KEY_LEFTMETA: return VK_LWIN;
        case KEY_RIGHTMETA: return VK_RWIN;
        case KEY_COMPOSE: return VK_APPS;
        case KEY_102ND: return VK_OEM_102;

        case KEY_SLEEP: return VK_SLEEP;
        case KEY_MUTE: return VK_VOLUME_MUTE;
        case KEY_VOLUMEDOWN: return VK_VOLUME_DOWN;
        case KEY_VOLUMEUP: return VK_VOLUME_UP;
        case KEY_NEXTSONG: return VK_MEDIA_NEXT_TRACK;
        case KEY_PREVIOUSSONG: return VK_MEDIA_PREV_TRACK;
        case KEY_STOPCD: return VK_MEDIA_STOP;
        case KEY_PLAYPAUSE: return VK_MEDIA_PLAY_PAUSE;
        case KEY_BACK: return VK_BROWSER_BACK;
        case KEY_FORWARD: return VK_BROWSER_FORWARD;
        case KEY_REFRESH: return VK_BROWSER_REFRESH;
        case KEY_HOMEPAGE: return VK_BROWSER_HOME;
#ifdef KEY_SEARCH
        case KEY_SEARCH: return VK_BROWSER_SEARCH;
#endif
#ifdef KEY_MAIL
        case KEY_MAIL: return VK_LAUNCH_MAIL;
#endif
#ifdef KEY_MEDIA
        case KEY_MEDIA: return VK_LAUNCH_MEDIA_SELECT;
#endif
        default: return 0;
    }
}

static bool is_aux_key(unsigned short code) {
    switch (code) {
        case KEY_SLEEP:
        case KEY_MUTE:
        case KEY_VOLUMEDOWN:
        case KEY_VOLUMEUP:
        case KEY_NEXTSONG:
        case KEY_PREVIOUSSONG:
        case KEY_STOPCD:
        case KEY_PLAYPAUSE:
        case KEY_BACK:
        case KEY_FORWARD:
        case KEY_REFRESH:
        case KEY_HOMEPAGE:
            return true;
#ifdef KEY_SEARCH
        case KEY_SEARCH: return true;
#endif
#ifdef KEY_MAIL
        case KEY_MAIL: return true;
#endif
#ifdef KEY_MEDIA
        case KEY_MEDIA: return true;
#endif
        default: return false;
    }
}

static char modifier_for_key(unsigned short code) {
    switch (code) {
        case KEY_LEFTSHIFT:
        case KEY_RIGHTSHIFT: return MODIFIER_SHIFT;
        case KEY_LEFTCTRL:
        case KEY_RIGHTCTRL: return MODIFIER_CTRL;
        case KEY_LEFTALT:
        case KEY_RIGHTALT: return MODIFIER_ALT;
        case KEY_LEFTMETA:
        case KEY_RIGHTMETA: return MODIFIER_META;
        default: return 0;
    }
}

static bool send_keyboard(ugreen_input_t *input, short vk, bool pressed, char modifiers,
                          bool require_accepting) {
    bool sent = false;
    SDL_LockMutex(input->session_lock);
    if (session_can_send_locked(input, require_accepting)) {
        LiSendKeyboardEvent((short) (0x8000 | vk),
                            pressed ? KEY_ACTION_DOWN : KEY_ACTION_UP,
                            modifiers);
        sent = true;
    }
    SDL_UnlockMutex(input->session_lock);
    return sent;
}

static void release_sent_input(ugreen_input_t *input, ugreen_runtime_state_t *state) {
    for (int code = 0; code <= KEY_MAX; ++code) {
        if (!state->sent_keys[code]) continue;

        short vk = linux_key_to_vk((unsigned short) code);
        if (vk != 0) {
            char mod = modifier_for_key((unsigned short) code);
            char release_mods = state->modifiers & (char) ~mod;
            send_keyboard(input, vk, false, release_mods, false);
        }
        state->sent_keys[code] = false;
    }

    for (int button = BUTTON_LEFT; button <= BUTTON_X2; ++button) {
        if (state->sent_mouse[button]) {
            send_mouse_button(input, button, false, false);
            state->sent_mouse[button] = false;
        }
    }
    state->modifiers = 0;
}

static void handle_key_event(ugreen_input_t *input, ugreen_runtime_state_t *state,
                             const struct input_event *event, bool aux) {
    if (event->code > KEY_MAX || (event->value != 0 && event->value != 1)) return;
    if (aux && !is_aux_key(event->code)) return;

    short vk = linux_key_to_vk(event->code);
    if (vk == 0) return;

    char mod = modifier_for_key(event->code);

    if (event->value == 1) {
        state->modifiers |= mod;
        if (!state->sent_keys[event->code] &&
            send_keyboard(input, vk, true, state->modifiers, true)) {
            state->sent_keys[event->code] = true;
        }
    } else {
        state->modifiers &= (char) ~mod;
        if (state->sent_keys[event->code]) {
            send_keyboard(input, vk, false, state->modifiers, false);
            state->sent_keys[event->code] = false;
        }
    }
}

static void handle_mouse_event(ugreen_input_t *input, ugreen_runtime_state_t *state,
                               ugreen_dev_t *device, const struct input_event *event) {
    if (event->type == EV_REL) {
        if (event->code == REL_X) device->dx += event->value;
        else if (event->code == REL_Y) device->dy += event->value;
        else if (event->code == REL_WHEEL) send_mouse_scroll(input, event->value, false);
        else if (event->code == REL_HWHEEL) send_mouse_scroll(input, event->value, true);
        return;
    }

    if (event->type == EV_KEY && (event->value == 0 || event->value == 1)) {
        int button = linux_button_to_moonlight(event->code);
        if (button == 0) return;

        if (event->value == 1) {
            if (!state->sent_mouse[button] &&
                send_mouse_button(input, button, true, true)) {
                state->sent_mouse[button] = true;
            }
        } else if (state->sent_mouse[button]) {
            send_mouse_button(input, button, false, false);
            state->sent_mouse[button] = false;
        }
        return;
    }

    if (event->type == EV_SYN && event->code == SYN_REPORT) {
        if (device->dx != 0 || device->dy != 0) {
            send_mouse_move(input, device->dx, device->dy);
            device->dx = 0;
            device->dy = 0;
        }
    }
}

static ugreen_dev_kind_t classify_device(int fd, const char *name) {
    unsigned long evbits[UGREEN_NBITS(EV_MAX + 1)];
    unsigned long keybits[UGREEN_NBITS(KEY_MAX + 1)];
    unsigned long relbits[UGREEN_NBITS(REL_MAX + 1)];

    memset(evbits, 0, sizeof(evbits));
    memset(keybits, 0, sizeof(keybits));
    memset(relbits, 0, sizeof(relbits));

    if (ioctl(fd, EVIOCGBIT(0, sizeof(evbits)), evbits) < 0) return UGREEN_DEV_OTHER;

    if (bit_is_set(EV_KEY, evbits)) {
        ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(keybits)), keybits);
    }
    if (bit_is_set(EV_REL, evbits)) {
        ioctl(fd, EVIOCGBIT(EV_REL, sizeof(relbits)), relbits);
    }

    if (bit_is_set(EV_REL, evbits) &&
        bit_is_set(REL_X, relbits) &&
        bit_is_set(REL_Y, relbits) &&
        bit_is_set(EV_KEY, evbits) &&
        bit_is_set(BTN_LEFT, keybits)) {
        return UGREEN_DEV_MOUSE;
    }

    if (bit_is_set(EV_KEY, evbits) &&
        bit_is_set(EV_LED, evbits) &&
        bit_is_set(KEY_A, keybits) &&
        bit_is_set(KEY_LEFTCTRL, keybits)) {
        return UGREEN_DEV_KEYBOARD;
    }

    if (bit_is_set(EV_KEY, evbits) &&
        name != NULL && strstr(name, "Keyboard") != NULL) {
        return UGREEN_DEV_AUX_KEYBOARD;
    }

    return UGREEN_DEV_OTHER;
}

static const char *kind_name(ugreen_dev_kind_t kind) {
    switch (kind) {
        case UGREEN_DEV_MOUSE: return "mouse";
        case UGREEN_DEV_KEYBOARD: return "keyboard";
        case UGREEN_DEV_AUX_KEYBOARD: return "aux-keyboard";
        default: return "aux";
    }
}

static int open_devices(ugreen_dev_t *devices, int max_devices) {
    DIR *dir = opendir("/dev/input");
    if (dir == NULL) {
        commons_log_warn("UGREEN", "Cannot open /dev/input: %s", strerror(errno));
        return 0;
    }

    int count = 0;
    struct dirent *entry;

    while ((entry = readdir(dir)) != NULL && count < max_devices) {
        if (strncmp(entry->d_name, "event", 5) != 0) continue;

        char path[64];
        snprintf(path, sizeof(path), "/dev/input/%s", entry->d_name);

        int fd = open(path, O_RDONLY | O_NONBLOCK);
        if (fd < 0) continue;

        struct input_id id;
        memset(&id, 0, sizeof(id));

        if (ioctl(fd, EVIOCGID, &id) < 0 ||
            id.vendor != UGREEN_VENDOR_ID ||
            id.product != UGREEN_PRODUCT_ID) {
            close(fd);
            continue;
        }

        char name[128];
        memset(name, 0, sizeof(name));
        ioctl(fd, EVIOCGNAME(sizeof(name) - 1), name);

        if (strncmp(name, "UGREEN Receiver", 15) != 0) {
            close(fd);
            continue;
        }

        if (ioctl(fd, EVIOCGRAB, 1) < 0) {
            commons_log_warn("UGREEN", "EVIOCGRAB failed for %s (%s): %s",
                             path, name, strerror(errno));
            close(fd);
            continue;
        }

        devices[count].fd = fd;
        devices[count].kind = classify_device(fd, name);
        devices[count].dx = 0;
        devices[count].dy = 0;
        snprintf(devices[count].path, sizeof(devices[count].path), "%s", path);
        snprintf(devices[count].name, sizeof(devices[count].name), "%s", name);

        commons_log_info("UGREEN", "Grabbed %s: %s (%s)",
                         kind_name(devices[count].kind), path, name);
        ++count;
    }

    closedir(dir);

    if (count > 0) {
        commons_log_info("UGREEN", "Exclusive input active on %d node(s)", count);
    }
    return count;
}

static void close_devices(ugreen_dev_t *devices, int count) {
    for (int i = 0; i < count; ++i) {
        if (devices[i].fd >= 0) {
            ioctl(devices[i].fd, EVIOCGRAB, 0);
            close(devices[i].fd);
            devices[i].fd = -1;
        }
    }

    if (count > 0) commons_log_info("UGREEN", "Exclusive input released");
}

static int ugreen_thread(void *opaque) {
    ugreen_input_t *input = opaque;
    ugreen_dev_t devices[UGREEN_MAX_DEVICES];
    ugreen_runtime_state_t state;

    memset(devices, 0, sizeof(devices));
    memset(&state, 0, sizeof(state));

    for (int i = 0; i < UGREEN_MAX_DEVICES; ++i) devices[i].fd = -1;

    int device_count = 0;

    while (SDL_AtomicGet(&input->running)) {
        if (!SDL_AtomicGet(&input->active)) {
            if (device_count > 0) {
                release_sent_input(input, &state);
                close_devices(devices, device_count);
                device_count = 0;
            }
            state.was_accepting = false;
            SDL_Delay(50);
            continue;
        }

        unsigned int generation = 0;
        bool accepting = ugreen_is_accepting(input, &generation);

        if (generation != state.session_generation) {
            memset(state.sent_keys, 0, sizeof(state.sent_keys));
            memset(state.sent_mouse, 0, sizeof(state.sent_mouse));
            state.modifiers = 0;
            state.session_generation = generation;
            state.was_accepting = accepting;
        } else if (state.was_accepting && !accepting) {
            release_sent_input(input, &state);
            state.was_accepting = false;
        } else if (accepting) {
            state.was_accepting = true;
        }

        if (device_count == 0) {
            device_count = open_devices(devices, UGREEN_MAX_DEVICES);
            if (device_count == 0) {
                SDL_Delay(500);
                continue;
            }
        }

        fd_set readfds;
        FD_ZERO(&readfds);
        int maxfd = -1;

        for (int i = 0; i < device_count; ++i) {
            if (devices[i].fd >= 0) {
                FD_SET(devices[i].fd, &readfds);
                if (devices[i].fd > maxfd) maxfd = devices[i].fd;
            }
        }

        struct timeval timeout;
        timeout.tv_sec = 0;
        timeout.tv_usec = 20000;

        int ready = select(maxfd + 1, &readfds, NULL, NULL, &timeout);
        if (ready < 0) {
            if (errno == EINTR) continue;
            release_sent_input(input, &state);
            close_devices(devices, device_count);
            device_count = 0;
            SDL_Delay(100);
            continue;
        }

        bool rescan = false;

        for (int i = 0; i < device_count && !rescan; ++i) {
            if (devices[i].fd < 0 || !FD_ISSET(devices[i].fd, &readfds)) continue;

            for (;;) {
                struct input_event event;
                ssize_t bytes = read(devices[i].fd, &event, sizeof(event));

                if (bytes == (ssize_t) sizeof(event)) {
                    switch (devices[i].kind) {
                        case UGREEN_DEV_MOUSE:
                            handle_mouse_event(input, &state, &devices[i], &event);
                            break;
                        case UGREEN_DEV_KEYBOARD:
                            if (event.type == EV_KEY) {
                                handle_key_event(input, &state, &event, false);
                            }
                            break;
                        case UGREEN_DEV_AUX_KEYBOARD:
                            if (event.type == EV_KEY) {
                                handle_key_event(input, &state, &event, true);
                            }
                            break;
                        default:
                            break;
                    }
                    continue;
                }

                if (bytes < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
                if (bytes < 0 && errno == EINTR) continue;

                rescan = true;
                break;
            }
        }

        if (rescan) {
            release_sent_input(input, &state);
            close_devices(devices, device_count);
            device_count = 0;
            SDL_Delay(100);
        }
    }

    release_sent_input(input, &state);
    close_devices(devices, device_count);
    return 0;
}

int ugreen_input_init(ugreen_input_t *input, app_t *app) {
    memset(input, 0, sizeof(*input));
    input->app = app;

    input->session_lock = SDL_CreateMutex();
    if (input->session_lock == NULL) {
        commons_log_error("UGREEN", "Failed to create session mutex: %s", SDL_GetError());
        return -1;
    }

    SDL_AtomicSet(&input->running, 1);
    SDL_AtomicSet(&input->active, 1);

    input->thread = SDL_CreateThread(ugreen_thread, "ugreen-input", input);
    if (input->thread == NULL) {
        commons_log_error("UGREEN", "Failed to start input thread: %s", SDL_GetError());
        SDL_AtomicSet(&input->running, 0);
        SDL_DestroyMutex(input->session_lock);
        input->session_lock = NULL;
        return -1;
    }

    commons_log_info("UGREEN", "Raw input manager started for 2b89:0043");
    return 0;
}

void ugreen_input_deinit(ugreen_input_t *input) {
    if (input == NULL) return;

    SDL_AtomicSet(&input->active, 0);
    SDL_AtomicSet(&input->running, 0);

    if (input->thread != NULL) {
        SDL_WaitThread(input->thread, NULL);
        input->thread = NULL;
    }

    if (input->session_lock != NULL) {
        SDL_DestroyMutex(input->session_lock);
        input->session_lock = NULL;
    }
}

void ugreen_input_set_active(ugreen_input_t *input, bool active) {
    if (input == NULL) return;
    SDL_AtomicSet(&input->active, active ? 1 : 0);
}

void ugreen_input_set_session(ugreen_input_t *input, session_t *session) {
    if (input == NULL || input->session_lock == NULL) return;

    SDL_LockMutex(input->session_lock);
    input->session = session;
    ++input->session_generation;
    SDL_UnlockMutex(input->session_lock);
}

#else

#include <string.h>

int ugreen_input_init(ugreen_input_t *input, app_t *app) {
    memset(input, 0, sizeof(*input));
    input->app = app;
    return 0;
}

void ugreen_input_deinit(ugreen_input_t *input) {
    (void) input;
}

void ugreen_input_set_active(ugreen_input_t *input, bool active) {
    (void) input;
    (void) active;
}

void ugreen_input_set_session(ugreen_input_t *input, session_t *session) {
    (void) input;
    (void) session;
}

#endif
'''

def main():
    replace_once(
        "src/app/input/CMakeLists.txt",
        "        input_gamepad_mapping.c)",
        "        input_gamepad_mapping.c\n        ugreen_input.c)"
    )

    replace_once(
        "src/app/input/app_input.h",
        '#include "lvgl/input/lv_drv_sdl_key.h"\n',
        '#include "lvgl/input/lv_drv_sdl_key.h"\n#include "ugreen_input.h"\n'
    )
    replace_once(
        "src/app/input/app_input.h",
        "    short activeGamepadMask;\n} app_input_t;",
        "    short activeGamepadMask;\n    ugreen_input_t ugreen;\n} app_input_t;"
    )

    replace_once(
        "src/app/input/app_input.c",
        "    app_input_init_gamepad_mapping(input, app->backend.executor, &app->settings);\n}",
        "    app_input_init_gamepad_mapping(input, app->backend.executor, &app->settings);\n"
        "    ugreen_input_init(&input->ugreen, app);\n}"
    )
    replace_once(
        "src/app/input/app_input.c",
        "void app_input_deinit(app_input_t *input) {\n"
        "    app_input_deinit_gamepad_mapping(input);",
        "void app_input_deinit(app_input_t *input) {\n"
        "    ugreen_input_deinit(&input->ugreen);\n"
        "    app_input_deinit_gamepad_mapping(input);"
    )

    replace_once(
        "src/app/app_session.c",
        "    app->session = session_create(app, app_configuration, node->server, gs_app);\n"
        "    return 0;",
        "    app->session = session_create(app, app_configuration, node->server, gs_app);\n"
        "    if (app->session != NULL) {\n"
        "        ugreen_input_set_session(&app->input.ugreen, app->session);\n"
        "    }\n"
        "    return 0;"
    )
    replace_once(
        "src/app/app_session.c",
        "    session_destroy(app->session);\n"
        "    app->session = NULL;",
        "    ugreen_input_set_session(&app->input.ugreen, NULL);\n"
        "    session_destroy(app->session);\n"
        "    app->session = NULL;"
    )

    replace_once(
        "src/app/app.c",
        "        case SDL_APP_WILLENTERBACKGROUND: {\n"
        "            // Interrupt streaming because app will go to background",
        "        case SDL_APP_WILLENTERBACKGROUND: {\n"
        "            ugreen_input_set_active(&app->input.ugreen, false);\n"
        "            // Interrupt streaming because app will go to background"
    )
    replace_once(
        "src/app/app.c",
        "        case SDL_APP_DIDENTERFOREGROUND: {\n"
        "            lv_obj_invalidate(lv_scr_act());",
        "        case SDL_APP_DIDENTERFOREGROUND: {\n"
        "            ugreen_input_set_active(&app->input.ugreen, true);\n"
        "            lv_obj_invalidate(lv_scr_act());"
    )

    replace_once(
        "src/app/stream/input/session_input.c",
        "    input->no_sdl_mouse = config->hardware_mouse;",
        "#if TARGET_WEBOS\n"
        "    input->no_sdl_mouse = true;\n"
        "#else\n"
        "    input->no_sdl_mouse = config->hardware_mouse;\n"
        "#endif"
    )

    replace_all(
        "src/app/stream/input/session_input.c",
        "#if FEATURE_INPUT_EVMOUSE",
        "#if FEATURE_INPUT_EVMOUSE && !TARGET_WEBOS",
        expected=5
    )

    replace_once(
        "src/app/stream/input/session_mouse.c",
        "void stream_input_handle_mbutton(stream_input_t *input, const SDL_MouseButtonEvent *event) {\n"
        "    if (pointer_gesture_handle_mbutton(input, event)) {",
        "void stream_input_handle_mbutton(stream_input_t *input, const SDL_MouseButtonEvent *event) {\n"
        "    if (input->no_sdl_mouse && event->which != SDL_TOUCH_MOUSEID) {\n"
        "        return;\n"
        "    }\n"
        "    if (pointer_gesture_handle_mbutton(input, event)) {"
    )
    replace_once(
        "src/app/stream/input/session_mouse.c",
        "void stream_input_handle_mwheel(stream_input_t *input, const SDL_MouseWheelEvent *event) {\n"
        "    (void) input;",
        "void stream_input_handle_mwheel(stream_input_t *input, const SDL_MouseWheelEvent *event) {\n"
        "    if (input->no_sdl_mouse && event->which != SDL_TOUCH_MOUSEID) {\n"
        "        return;\n"
        "    }\n"
        "    (void) input;"
    )

    (ROOT / "src/app/input/ugreen_input.h").write_text(UGREEN_H, encoding="utf-8")
    (ROOT / "src/app/input/ugreen_input.c").write_text(UGREEN_C, encoding="utf-8")
    print("created src/app/input/ugreen_input.h")
    print("created src/app/input/ugreen_input.c")
    print("\nUGREEN modification applied successfully.")
    print("Target receiver: VID 2b89 / PID 0043")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
