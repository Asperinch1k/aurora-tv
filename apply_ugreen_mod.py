#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Aurora USB + SAS relay v2. Replace the old same-named generator in the fork.
All source anchors are validated before writing. --check does not modify files.
The Windows helper in the accompanying kit is required for the SAS screen.
"""
from pathlib import Path
import argparse
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
STAGED = {}
ORIGINALS = {}
CHECK = False

def load(rel):
    if rel not in STAGED:
        p = ROOT / rel
        if not p.is_file():
            raise RuntimeError(f"Missing source file: {rel}. Run this in the Aurora source root.")
        ORIGINALS[rel] = p.read_bytes()
        STAGED[rel] = ORIGINALS[rel].decode("utf-8").replace("\r\n", "\n")
    return STAGED[rel]

def replace_all(rel, old, new, expected=None):
    text = load(rel)
    parts = text.split(new)  # Do not re-match an old anchor embedded in new text.
    already = len(parts) - 1
    remaining = sum(part.count(old) for part in parts)
    total = already + remaining
    if not total or (expected is not None and total != expected):
        raise RuntimeError(f"{rel}: incompatible source anchor: {remaining} old + {already} patched; expected {expected}. No source files changed.")
    STAGED[rel] = new.join(part.replace(old, new) for part in parts)

def replace_once(rel, old, new):
    replace_all(rel, old, new, expected=1)

def stage(rel, text):
    p = ROOT / rel
    if not p.parent.is_dir():
        raise RuntimeError(f"Missing directory: {p.parent}")
    if rel not in ORIGINALS:
        ORIGINALS[rel] = p.read_bytes() if p.exists() else None
    STAGED[rel] = text

def atomic_write(path, data):
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, name = tempfile.mkstemp(prefix=".aurora-usb-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def commit():
    changed = [rel for rel, text in STAGED.items() if ORIGINALS[rel] != text.encode("utf-8")]
    if CHECK:
        print("CHECK OK; no files written. Would update:")
        for rel in changed: print("  " + rel)
        return
    written = []
    try:
        for rel in changed:
            atomic_write(ROOT / rel, STAGED[rel].encode("utf-8"))
            written.append(rel)
    except Exception:
        for rel in reversed(written):
            original = ORIGINALS[rel]
            try:
                if original is None: (ROOT / rel).unlink()
                else: atomic_write(ROOT / rel, original)
            except Exception as rollback_error:
                print(f"ROLLBACK ERROR {rel}: {rollback_error}", file=sys.stderr)
        raise
    for rel in changed: print("updated " + rel)
    print("Aurora USB + UGREEN + SAS relay v2 applied; Windows SAS helper required.")
    if not changed: print("Already up to date.")

UGREEN_H = r'''#pragma once

#include <SDL.h>
#include <stdbool.h>

typedef struct app_t app_t;
typedef struct session_t session_t;

/*
 * USB keyboard/mouse raw input, retaining UGREEN 2b89:0043 support.
 * Captured nodes are exclusively grabbed only while Aurora is foregrounded.
 * Sending requires an active non-view-only stream. LG remote/gamepads excluded.
 * Ctrl+Alt+Delete / Ctrl+Alt+End requests the Windows SAS relay helper.
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

UGREEN_C = r'''/* SPDX-License-Identifier: GPL-3.0-or-later
 * Personal Aurora USB input / SAS relay modification, revision 2.
 * SAS uses a reserved ordinary shortcut; a Windows service supplies SendSAS.
 * No new network listener or host credentials are added to Aurora.
 */
#include "config.h"
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
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/ioctl.h>
#include <sys/select.h>
#include <unistd.h>
#include "logging.h"
#include "stream/input/vk.h"
#include "stream/session.h"

#define RAW_MAX_DEVICES 32
#define RAW_MAX_CANDIDATES 64
#define RAW_SCAN_MS 1000
#define BPL (sizeof(unsigned long) * 8)
#define NBITS(n) (((n) + BPL - 1) / BPL)
#define RAW_MOUSE 1
#define RAW_KEYBOARD 2
#define RAW_AUX 4
#define RAW_VK_F24 0x87

typedef struct {
    int fd;
    char path[64], name[128], group[128];
    unsigned int kind;
    bool known_ugreen, has_mapped_keys, dropped;
    bool held[KEY_MAX + 1], blocked[KEY_MAX + 1];
    int64_t dx, dy;
} raw_device_t;
typedef struct {
    unsigned short keys[KEY_MAX + 1], buttons[6];
    bool sent_keys[KEY_MAX + 1], sent_buttons[6];
    bool sas_latched, was_accepting;
    unsigned int generation;
} raw_state_t;

static bool bit_set(unsigned int bit, const unsigned long *bits) {
    return (bits[bit / BPL] & (1UL << (bit % BPL))) != 0;
}
static bool name_contains(const char *name, const char *part) {
    size_t n = strlen(part);
    for (; *name; ++name) if (strncasecmp(name, part, n) == 0) return true;
    return false;
}
static bool excluded_name(const char *name) {
    const char *exclude[] = {"LGE RCU", "DualSense", "DualShock", "Wireless Controller",
                             "gamepad", "joystick", "Xbox", "webOS", "Luna"};
    for (size_t i = 0; i < sizeof(exclude)/sizeof(exclude[0]); ++i)
        if (name_contains(name, exclude[i])) return true;
    return false;
}
static bool can_send_locked(ugreen_input_t *input, bool down) {
    if (!input->session) return false;
    stream_input_t *stream = session_get_input(input->session);
    if (!stream) return false;
    if (down) return SDL_AtomicGet(&input->active) && !stream->view_only &&
                     session_accepting_input(input->session);
    /* Previously sent releases must still be allowed when entering view-only. */
    return session_has_input(input->session);
}
static bool is_accepting(ugreen_input_t *input, unsigned int *generation) {
    bool ok;
    SDL_LockMutex(input->session_lock);
    *generation = input->session_generation;
    ok = can_send_locked(input, true);
    SDL_UnlockMutex(input->session_lock);
    return ok;
}
static bool send_key(ugreen_input_t *input, short vk, bool down, char mods) {
    bool ok = false;
    SDL_LockMutex(input->session_lock);
    if (can_send_locked(input, down)) {
        ok = LiSendKeyboardEvent((short)(0x8000 | vk),
                                down ? KEY_ACTION_DOWN : KEY_ACTION_UP, mods) == 0;
    }
    SDL_UnlockMutex(input->session_lock);
    return ok;
}
static int mouse_button(unsigned short code) {
    switch (code) {
        case BTN_LEFT: return BUTTON_LEFT;
        case BTN_MIDDLE: return BUTTON_MIDDLE;
        case BTN_RIGHT: return BUTTON_RIGHT;
        case BTN_SIDE: case BTN_BACK: return BUTTON_X1;
        case BTN_EXTRA: case BTN_FORWARD: return BUTTON_X2;
        default: return 0;
    }
}
static bool send_button(ugreen_input_t *input, int button, bool down) {
    bool ok = false;
    SDL_LockMutex(input->session_lock);
    if (can_send_locked(input, down))
        ok = LiSendMouseButtonEvent(down ? BUTTON_ACTION_PRESS : BUTTON_ACTION_RELEASE,
                                   button) == 0;
    SDL_UnlockMutex(input->session_lock);
    return ok;
}
static void send_motion(ugreen_input_t *input, int64_t x, int64_t y) {
    if (!x && !y) return;
    if (x > 32767) x = 32767;
    if (x < -32768) x = -32768;
    if (y > 32767) y = 32767;
    if (y < -32768) y = -32768;
    SDL_LockMutex(input->session_lock);
    if (can_send_locked(input, true)) LiSendMouseMoveEvent((short)x, (short)y);
    SDL_UnlockMutex(input->session_lock);
}
static void send_scroll(ugreen_input_t *input, int value, bool horizontal) {
    if (!value) return;
    if (value > 127) value = 127;
    if (value < -127) value = -127;
    SDL_LockMutex(input->session_lock);
    if (can_send_locked(input, true)) {
        if (horizontal) LiSendHScrollEvent((signed char)value);
        else LiSendScrollEvent((signed char)value);
    }
    SDL_UnlockMutex(input->session_lock);
}
static short linux_key_to_vk(unsigned short code) {
    if (code >= KEY_F1 && code <= KEY_F10) {
        return (short) (VK_F1 + (code - KEY_F1));
    }

#ifdef KEY_F13
    if (code >= KEY_F13 && code <= KEY_F24) {
        return (short) (0x7c + code - KEY_F13);
    }
#endif
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

static char current_mods(const raw_state_t *s) {
    char result = 0;
    if (s->keys[KEY_LEFTCTRL] || s->keys[KEY_RIGHTCTRL]) result |= MODIFIER_CTRL;
    if (s->keys[KEY_LEFTALT] || s->keys[KEY_RIGHTALT]) result |= MODIFIER_ALT;
    if (s->keys[KEY_LEFTSHIFT] || s->keys[KEY_RIGHTSHIFT]) result |= MODIFIER_SHIFT;
    if (s->keys[KEY_LEFTMETA] || s->keys[KEY_RIGHTMETA]) result |= MODIFIER_META;
    return result;
}
static bool is_modifier(unsigned int code) {
    return code == KEY_LEFTCTRL || code == KEY_RIGHTCTRL || code == KEY_LEFTALT ||
           code == KEY_RIGHTALT || code == KEY_LEFTSHIFT || code == KEY_RIGHTSHIFT ||
           code == KEY_LEFTMETA || code == KEY_RIGHTMETA;
}
static void release_all(ugreen_input_t *input, raw_state_t *s) {
    /* Non-modifier releases first, then release all modifier keys. */
    for (int pass = 0; pass < 2; ++pass) {
        for (unsigned int code = 0; code <= KEY_MAX; ++code) {
            if (!s->sent_keys[code] || is_modifier(code) != (pass == 1)) continue;
            send_key(input, linux_key_to_vk((unsigned short)code), false, 0);
            s->sent_keys[code] = false;
        }
    }
    for (int b = BUTTON_LEFT; b <= BUTTON_X2; ++b) {
        if (s->sent_buttons[b]) send_button(input, b, false);
        s->sent_buttons[b] = false;
    }
}
static void clear_physical(raw_state_t *s, raw_device_t *devices, int count) {
    memset(s->keys, 0, sizeof(s->keys));
    memset(s->buttons, 0, sizeof(s->buttons));
    s->sas_latched = false;
    for (int i = 0; i < count; ++i) {
        memset(devices[i].held, 0, sizeof(devices[i].held));
        devices[i].dx = devices[i].dy = 0;
    }
}
static void send_sas_marker(ugreen_input_t *input) {
    /* Ctrl+Alt+Shift+F24 is not a new Moonlight protocol packet. Sunshine
       transports it normally; AuroraSasRelay on Windows performs SendSAS. */
    SDL_LockMutex(input->session_lock);
    if (can_send_locked(input, true)) {
        const short vk[] = {VK_LCONTROL, VK_LMENU, VK_LSHIFT, RAW_VK_F24};
        const char mod[] = {MODIFIER_CTRL, MODIFIER_CTRL | MODIFIER_ALT,
                           MODIFIER_CTRL | MODIFIER_ALT | MODIFIER_SHIFT,
                           MODIFIER_CTRL | MODIFIER_ALT | MODIFIER_SHIFT};
        for (int i = 0; i < 4; ++i)
            LiSendKeyboardEvent((short)(0x8000 | vk[i]), KEY_ACTION_DOWN, mod[i]);
        for (int i = 3; i >= 0; --i)
            LiSendKeyboardEvent((short)(0x8000 | vk[i]), KEY_ACTION_UP,
                                i == 0 ? 0 : mod[i-1]);
        commons_log_info("USBINPUT", "SAS relay shortcut queued (host helper required; no acknowledgement)");
    }
    SDL_UnlockMutex(input->session_lock);
}
static bool update_held(raw_device_t *dev, unsigned int code, int value) {
    if (code > KEY_MAX || (value != 0 && value != 1)) return false;
    if (dev->blocked[code]) {
        if (!value) dev->blocked[code] = false;
        return false;
    }
    bool down = value != 0;
    if (dev->held[code] == down) return false;
    dev->held[code] = down;
    return true;
}
static void handle_key(ugreen_input_t *input, raw_state_t *s, raw_device_t *dev,
                       unsigned int code, int value) {
    short vk = linux_key_to_vk((unsigned short)code);
    if (vk == 0 || !update_held(dev, code, value)) return;
    if (value) ++s->keys[code];
    else if (s->keys[code]) --s->keys[code];
    char mods = current_mods(s);
    if (s->sas_latched) {
        if (!(mods & (MODIFIER_CTRL | MODIFIER_ALT)) &&
            !s->keys[KEY_DELETE] && !s->keys[KEY_END]) s->sas_latched = false;
        return;  /* Suppress this chord until all three physical keys are up. */
    }
    unsigned int gen;
    if (value && (code == KEY_DELETE || code == KEY_END) &&
        (mods & (MODIFIER_CTRL | MODIFIER_ALT)) == (MODIFIER_CTRL | MODIFIER_ALT) &&
        !(mods & (MODIFIER_SHIFT | MODIFIER_META)) && is_accepting(input, &gen)) {
        release_all(input, s);
        s->sas_latched = true;
        send_sas_marker(input);
        return;
    }
    if (value && !s->sent_keys[code]) s->sent_keys[code] = send_key(input, vk, true, mods);
    else if (!value && s->keys[code] == 0 && s->sent_keys[code]) {
        send_key(input, vk, false, mods);
        s->sent_keys[code] = false;
    }
}
static void handle_button(ugreen_input_t *input, raw_state_t *s, raw_device_t *dev,
                          unsigned int code, int value) {
    int b = mouse_button((unsigned short)code);
    if (!b || !update_held(dev, code, value)) return;
    if (value) ++s->buttons[b];
    else if (s->buttons[b]) --s->buttons[b];
    if (s->sas_latched) return;
    if (value && !s->sent_buttons[b]) s->sent_buttons[b] = send_button(input, b, true);
    else if (!value && !s->buttons[b] && s->sent_buttons[b]) {
        send_button(input, b, false);
        s->sent_buttons[b] = false;
    }
}
static void forget_device(ugreen_input_t *input, raw_state_t *s, raw_device_t *dev) {
    for (unsigned int code = 0; code <= KEY_MAX; ++code) {
        if (!dev->held[code]) continue;
        int b = mouse_button((unsigned short)code);
        if (b) handle_button(input, s, dev, code, 0);
        else handle_key(input, s, dev, code, 0);
    }
    dev->dx = dev->dy = 0;
}
static void block_held_keys(raw_device_t *dev) {
    unsigned long keys[NBITS(KEY_MAX + 1)] = {0};
    memset(dev->blocked, 0, sizeof(dev->blocked));
    if (ioctl(dev->fd, EVIOCGKEY(sizeof(keys)), keys) >= 0)
        for (unsigned int i = 0; i <= KEY_MAX; ++i) dev->blocked[i] = bit_set(i, keys);
}
static void process_event(ugreen_input_t *input, raw_state_t *s, raw_device_t *dev,
                          const struct input_event *ev) {
    if (ev->type == EV_SYN && ev->code == SYN_DROPPED) {
        forget_device(input, s, dev);
        dev->dropped = true;
        return;
    }
    if (dev->dropped) {
        if (ev->type == EV_SYN && ev->code == SYN_REPORT) {
            block_held_keys(dev);  /* Do not replay stale presses after overrun. */
            dev->dropped = false;
        }
        return;
    }
    if (ev->type == EV_KEY && ev->code <= KEY_MAX) {
        if (mouse_button(ev->code) && (dev->kind & RAW_MOUSE))
            handle_button(input, s, dev, ev->code, ev->value);
        else if (dev->kind & (RAW_KEYBOARD | RAW_AUX))
            handle_key(input, s, dev, ev->code, ev->value);
    } else if (ev->type == EV_REL && (dev->kind & RAW_MOUSE) && !s->sas_latched) {
        if (ev->code == REL_X) dev->dx += ev->value;
        else if (ev->code == REL_Y) dev->dy += ev->value;
        else if (ev->code == REL_WHEEL) send_scroll(input, ev->value, false);
        else if (ev->code == REL_HWHEEL) send_scroll(input, ev->value, true);
    } else if (ev->type == EV_SYN && ev->code == SYN_REPORT) {
        if (!s->sas_latched) send_motion(input, dev->dx, dev->dy);
        dev->dx = dev->dy = 0;
    }
}
/* This function is also covered by the host-side capability unit tests. */
static unsigned int classify_bits(const unsigned long *evbits,
                                  const unsigned long *keys, const unsigned long *rel) {
    if (!bit_set(EV_KEY, evbits)) return 0;
    for (unsigned int i = BTN_JOYSTICK; i < BTN_DIGI; ++i)
        if (bit_set(i, keys)) return 0;  /* Gamepads and joysticks, not USB mice. */
    unsigned int kind = 0;
    if (bit_set(EV_REL, evbits) && bit_set(REL_X, rel) && bit_set(REL_Y, rel) &&
        bit_set(BTN_LEFT, keys)) kind |= RAW_MOUSE;
    if (bit_set(KEY_A, keys) && bit_set(KEY_Z, keys) && bit_set(KEY_Q, keys) &&
        bit_set(KEY_ENTER, keys) && bit_set(KEY_SPACE, keys) && bit_set(KEY_LEFTCTRL, keys))
        kind |= RAW_KEYBOARD;  /* EV_LED is not required. */
    return kind;
}
static bool describe_device(raw_device_t *dev) {
    struct input_id id = {0};
    unsigned long evbits[NBITS(EV_MAX + 1)] = {0};
    unsigned long keys[NBITS(KEY_MAX + 1)] = {0}, rel[NBITS(REL_MAX + 1)] = {0};
    if (ioctl(dev->fd, EVIOCGID, &id) < 0 || id.bustype != BUS_USB) return false;
    ioctl(dev->fd, EVIOCGNAME(sizeof(dev->name)), dev->name);
    dev->name[sizeof(dev->name)-1] = 0;
    if (excluded_name(dev->name)) return false;
    if (id.vendor == 0x054c && (id.product == 0x0ce6 || id.product == 0x0df2)) return false;
    if (ioctl(dev->fd, EVIOCGBIT(0, sizeof(evbits)), evbits) < 0) return false;
    if (bit_set(EV_KEY, evbits) && ioctl(dev->fd, EVIOCGBIT(EV_KEY, sizeof(keys)), keys) < 0)
        return false;
    if (bit_set(EV_REL, evbits) && ioctl(dev->fd, EVIOCGBIT(EV_REL, sizeof(rel)), rel) < 0)
        return false;
    /* Reject controller interfaces even when they also advertise keyboard keys. */
    for (unsigned int i = BTN_JOYSTICK; i < BTN_DIGI; ++i)
        if (bit_set(i, keys)) return false;
    dev->kind = classify_bits(evbits, keys, rel);
    dev->known_ugreen = id.vendor == 0x2b89 && id.product == 0x0043;
    for (unsigned int i = 0; i <= KEY_MAX; ++i)
        if (bit_set(i, keys) && linux_key_to_vk((unsigned short)i)) dev->has_mapped_keys = true;
    ioctl(dev->fd, EVIOCGPHYS(sizeof(dev->group)), dev->group);
    dev->group[sizeof(dev->group)-1] = 0;
    char *suffix = strstr(dev->group, "/input");
    if (suffix) *suffix = 0;
    else dev->group[0] = 0;  /* No safe grouping information. */
    return dev->kind != 0 || dev->has_mapped_keys;
}
static bool same_group(const raw_device_t *a, const raw_device_t *b) {
    return a->group[0] && b->group[0] && strcmp(a->group, b->group) == 0;
}
static int scan_devices(raw_device_t *devices, int count) {
    DIR *dir = opendir("/dev/input");
    if (!dir) return count;
    raw_device_t *candidates = calloc(RAW_MAX_CANDIDATES, sizeof(*candidates));
    if (!candidates) { closedir(dir); return count; }
    int n = 0;
    struct dirent *entry;
    while (n < RAW_MAX_CANDIDATES && (entry = readdir(dir)) != NULL) {
        if (strncmp(entry->d_name, "event", 5) || !entry->d_name[5]) continue;
        bool digits = true;
        for (const char *p = entry->d_name + 5; *p; ++p)
            if (*p < '0' || *p > '9') digits = false;
        if (!digits) continue;
        char path[64];
        int len = snprintf(path, sizeof(path), "/dev/input/%s", entry->d_name);
        if (len < 0 || len >= (int)sizeof(path)) continue;
        bool existing = false;
        for (int i = 0; i < count; ++i) if (strcmp(path, devices[i].path) == 0) existing = true;
        if (existing) continue;
        int fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
        if (fd < 0) continue;
        if (fd >= FD_SETSIZE) { close(fd); continue; }
        raw_device_t *dev = &candidates[n];
        dev->fd = fd;
        memcpy(dev->path, path, (size_t)len + 1);
        if (!describe_device(dev)) { close(fd); memset(dev, 0, sizeof(*dev)); continue; }
        ++n;
    }
    closedir(dir);
    for (int i = 0; i < n; ++i) {
        raw_device_t *dev = &candidates[i];
        bool sibling = false;
        for (int j = 0; j < n; ++j)
            if ((candidates[j].kind & (RAW_KEYBOARD | RAW_MOUSE)) && same_group(dev, &candidates[j]))
                sibling = true;
        for (int j = 0; j < count; ++j)
            if ((devices[j].kind & (RAW_KEYBOARD | RAW_MOUSE)) && same_group(dev, &devices[j]))
                sibling = true;
        if (dev->has_mapped_keys && (dev->known_ugreen || sibling ||
            name_contains(dev->name, "keyboard"))) dev->kind |= RAW_AUX;
        if (!dev->kind || count == RAW_MAX_DEVICES) { close(dev->fd); continue; }
        if (ioctl(dev->fd, EVIOCGRAB, 1) < 0) {
            commons_log_warn("USBINPUT", "Cannot grab %s (%s): %s", dev->path, dev->name, strerror(errno));
            close(dev->fd);
            continue;
        }
        block_held_keys(dev);
        devices[count++] = *dev;
        commons_log_info("USBINPUT", "Grabbed %s: %s [kind=%u]", dev->path, dev->name, dev->kind);
    }
    free(candidates);
    return count;
}
static void close_devices(raw_device_t *devices, int count) {
    for (int i = 0; i < count; ++i) {
        ioctl(devices[i].fd, EVIOCGRAB, 0);
        close(devices[i].fd);
        devices[i].fd = -1;
    }
    if (count) commons_log_info("USBINPUT", "Exclusive USB input released");
}
static int raw_thread(void *opaque) {
    ugreen_input_t *input = opaque;
    raw_device_t *devices = calloc(RAW_MAX_DEVICES, sizeof(*devices));
    if (!devices) {
        commons_log_error("USBINPUT", "Cannot allocate input device state");
        return -1;
    }
    raw_state_t state;
    memset(&state, 0, sizeof(state));
    int count = 0;
    Uint32 last_scan = SDL_GetTicks() - RAW_SCAN_MS;
    while (SDL_AtomicGet(&input->running)) {
        if (!SDL_AtomicGet(&input->active)) {
            release_all(input, &state);
            clear_physical(&state, devices, count);
            close_devices(devices, count);
            count = 0;
            state.was_accepting = false;
            last_scan = SDL_GetTicks() - RAW_SCAN_MS;
            SDL_Delay(50);
            continue;
        }
        unsigned int generation;
        bool accepting = is_accepting(input, &generation);
        if (generation != state.generation) {
            memset(state.sent_keys, 0, sizeof(state.sent_keys));
            memset(state.sent_buttons, 0, sizeof(state.sent_buttons));
            clear_physical(&state, devices, count);
            state.generation = generation;
        } else if (state.was_accepting && !accepting) {
            release_all(input, &state);
            clear_physical(&state, devices, count);
        }
        state.was_accepting = accepting;
        Uint32 now = SDL_GetTicks();
        if ((Uint32)(now - last_scan) >= RAW_SCAN_MS) {
            count = scan_devices(devices, count);
            last_scan = now;
        }
        if (!count) { SDL_Delay(50); continue; }
        fd_set fds;
        FD_ZERO(&fds);
        int maxfd = -1;
        for (int i = 0; i < count; ++i) {
            FD_SET(devices[i].fd, &fds);
            if (devices[i].fd > maxfd) maxfd = devices[i].fd;
        }
        struct timeval timeout = {0, 20000};
        int ready = select(maxfd + 1, &fds, NULL, NULL, &timeout);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) {
            release_all(input, &state);
            clear_physical(&state, devices, count);
            close_devices(devices, count);
            count = 0;
            continue;
        }
        for (int i = 0; i < count;) {
            raw_device_t *dev = &devices[i];
            if (!FD_ISSET(dev->fd, &fds)) { ++i; continue; }
            bool gone = false;
            for (int budget = 0; budget < 512; ++budget) {
                struct input_event ev;
                ssize_t bytes = read(dev->fd, &ev, sizeof(ev));
                if (bytes == (ssize_t)sizeof(ev)) { process_event(input, &state, dev, &ev); continue; }
                if (bytes < 0 && errno == EINTR) continue;
                if (bytes < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
                gone = true;
                break;
            }
            if (!gone) { ++i; continue; }
            forget_device(input, &state, dev);
            ioctl(dev->fd, EVIOCGRAB, 0);
            close(dev->fd);
            commons_log_info("USBINPUT", "Disconnected %s", dev->path);
            devices[i] = devices[--count];
            last_scan = SDL_GetTicks() - RAW_SCAN_MS;
        }
    }
    release_all(input, &state);
    close_devices(devices, count);
    free(devices);
    return 0;
}
int ugreen_input_init(ugreen_input_t *input, app_t *app) {
    memset(input, 0, sizeof(*input));
    input->app = app;
    input->session_lock = SDL_CreateMutex();
    if (!input->session_lock) return -1;
    SDL_AtomicSet(&input->running, 1);
    SDL_AtomicSet(&input->active, 1);
    input->thread = SDL_CreateThread(raw_thread, "aurora-usb-input", input);
    if (!input->thread) {
        SDL_AtomicSet(&input->running, 0);
        SDL_DestroyMutex(input->session_lock);
        input->session_lock = NULL;
        return -1;
    }
    commons_log_info("USBINPUT", "USB + UGREEN raw-input v2 started; SAS relay enabled");
    return 0;
}
void ugreen_input_deinit(ugreen_input_t *input) {
    if (!input) return;
    SDL_AtomicSet(&input->active, 0);
    SDL_AtomicSet(&input->running, 0);
    if (input->thread) { SDL_WaitThread(input->thread, NULL); input->thread = NULL; }
    if (input->session_lock) { SDL_DestroyMutex(input->session_lock); input->session_lock = NULL; }
}
void ugreen_input_set_active(ugreen_input_t *input, bool active) {
    if (input) SDL_AtomicSet(&input->active, active ? 1 : 0);
}
void ugreen_input_set_session(ugreen_input_t *input, session_t *session) {
    if (!input || !input->session_lock) return;
    SDL_LockMutex(input->session_lock);
    input->session = session;
    ++input->session_generation;
    SDL_UnlockMutex(input->session_lock);
}
#else
#include <string.h>
int ugreen_input_init(ugreen_input_t *input, app_t *app) {
    memset(input, 0, sizeof(*input)); input->app = app; return 0;
}
void ugreen_input_deinit(ugreen_input_t *input) { (void)input; }
void ugreen_input_set_active(ugreen_input_t *input, bool active) { (void)input; (void)active; }
void ugreen_input_set_session(ugreen_input_t *input, session_t *session) { (void)input; (void)session; }
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
    stage("src/app/input/ugreen_input.h", UGREEN_H)
    stage("src/app/input/ugreen_input.c", UGREEN_C)
    commit()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Aurora source checkout")
    parser.add_argument("--check", action="store_true", help="Validate anchors without writing")
    args = parser.parse_args()
    ROOT = args.root.resolve()
    CHECK = args.check
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
