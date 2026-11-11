// probe3: replay the Linux xpad startup on macOS as closely as libusb allows, with timestamps.
//
// Linux (usbmon, ultimate_c_xpad_linux.pcap), all within ~10 ms of SET_CONFIGURATION:
//   SET_CONFIGURATION(1) -> SET_INTERFACE(intf 1, alt 0) -> IN urb already queued on 0x81
//   -> OUT 05 20 00 01 00 -> OUT 0a 20 01 03 00 01 14 -> OUT 06 20 02 02 01 00
//   <- 03 20 01 04 80 00 00 00 (status) <- 20 00 01 20 ... (input reports)
//
// Usage: probe3 [--wait MS] [--no-alt] [--reset] [--seconds N]
//   --wait MS   leave the device alone for MS after it appears (tests whether macOS darkens it on its own)
//   --no-alt    skip SET_INTERFACE(1,0)
//   --attached  use a controller that is already plugged in (default: wait for unplug, then a fresh plug-in)
//   --dup       send POWER ON twice (tests for a dropped first OUT packet / data-toggle mismatch)
//   --clear     libusb_clear_halt on 0x02 and 0x81 before the handshake (resets toggles on both ends)
//   --enum      replay Linux's enumeration descriptor requests before SET_CONFIGURATION (host fingerprint test)
//   --rumble    after the handshake, rumble both motors for ~1 s (tests whether the firmware acts on our OUT packets)
//   --reset     libusb_reset_device first (re-enumerates), then run the sequence immediately
// Build (macOS): cc -O2 probe3.c -o probe3 -I/opt/homebrew/include/libusb-1.0 -L/opt/homebrew/lib -lusb-1.0
// Build (Fedora): sudo dnf install gcc libusb1-devel && cc -O2 probe3.c -o probe3 $(pkg-config --cflags --libs libusb-1.0)
// Run on Linux as root; it detaches xpad from interface 0 by itself.
#include <libusb.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <unistd.h>

static double t0;
static double now(void) { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return ts.tv_sec + ts.tv_nsec / 1e9; }
#define LOG(...) do { printf("%8.1f ms  ", (now() - t0) * 1000); printf(__VA_ARGS__); printf("\n"); fflush(stdout); } while (0)

static libusb_device_handle *h;
static int total, shown, inputs;
static volatile int done;

static void hexline(const char *dir, const uint8_t *b, int n) {
    char s[256] = {0}; int o = 0;
    for (int i = 0; i < n && i < 40 && o < 240; i++) o += snprintf(s + o, sizeof s - o, " %02x", b[i]);
    LOG("%s%s%s", dir, s, n > 40 ? " ..." : "");
}

static void LIBUSB_CALL on_in(struct libusb_transfer *t) {
    if (t->status == LIBUSB_TRANSFER_COMPLETED) {
        total++;
        if (t->actual_length && t->buffer[0] == 0x20) inputs++;
        if (shown < 25 && t->actual_length) { shown++; hexline("<-", t->buffer, t->actual_length); }
        if (!done && libusb_submit_transfer(t) == 0) return;
    } else if (t->status != LIBUSB_TRANSFER_CANCELLED) {
        LOG("IN transfer ended: status %d", t->status);
    }
    done = 1;
}

static void sendpkt(const char *name, const uint8_t *p, int len) {
    int n = 0;
    int r = libusb_interrupt_transfer(h, 0x02, (uint8_t *)p, len, &n, 1000);
    hexline("->", p, len);
    LOG("   %s: %s (%d bytes)", name, libusb_error_name(r), n);
}


// Replay the descriptor requests Linux makes during enumeration (from the usbmon pcap), in order and with the same wLength.
static void replay_linux_enum(void) {
    static const struct { uint16_t val, idx, len; const char *what; } q[] = {
        {0x0100, 0, 64, "device(64)"}, {0x0100, 0, 18, "device(18)"},
        {0x0600, 0, 10, "qualifier"}, {0x0600, 0, 10, "qualifier"}, {0x0600, 0, 10, "qualifier"},
        {0x0200, 0, 9, "config(9)"}, {0x0200, 0, 64, "config(64)"},
        {0x0300, 0, 255, "string0"}, {0x0302, 0x0409, 255, "string2"},
        {0x0301, 0x0409, 255, "string1"}, {0x0303, 0x0409, 255, "string3"},
    };
    uint8_t b[255];
    for (unsigned i = 0; i < sizeof q / sizeof q[0]; i++) {
        int r = libusb_control_transfer(h, 0x80, 0x06, q[i].val, q[i].idx, b, q[i].len, 500);
        LOG("GET_DESCRIPTOR %-10s -> %s", q[i].what, r >= 0 ? "ok" : libusb_error_name(r));
    }
}

static libusb_device_handle *wait_open(libusb_context *ctx) {
    libusb_device_handle *d;
    while ((d = libusb_open_device_with_vid_pid(ctx, 0x2dc8, 0x2027)) == NULL) usleep(5000);
    return d;
}

int main(int argc, char **argv) {
    int wait_ms = 0, alt = 1, reset = 0, seconds = 8, fresh = 1, dup = 0, clear = 0, enumr = 0, rumble = 0, norwu = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--wait") && i + 1 < argc) wait_ms = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--no-alt")) alt = 0;
        else if (!strcmp(argv[i], "--reset")) reset = 1;
        else if (!strcmp(argv[i], "--attached")) fresh = 0;
        else if (!strcmp(argv[i], "--dup")) dup = 1;
        else if (!strcmp(argv[i], "--clear")) clear = 1;
        else if (!strcmp(argv[i], "--enum")) enumr = 1;
        else if (!strcmp(argv[i], "--rumble")) rumble = 1;
        else if (!strcmp(argv[i], "--norwu")) norwu = 1;
        else if (!strcmp(argv[i], "--seconds") && i + 1 < argc) seconds = atoi(argv[++i]);
    }
    libusb_context *ctx;
    libusb_init(&ctx);
    if (fresh) {
        libusb_device_handle *d;
        int said = 0;
        while ((d = libusb_open_device_with_vid_pid(ctx, 0x2dc8, 0x2027)) != NULL) {
            libusb_close(d);
            if (!said++) { printf("controller already attached: unplug it\n"); fflush(stdout); }
            usleep(100000);
        }
    }
    printf("waiting for 2dc8:2027 (plug it in now) wait=%d alt=%d reset=%d\n", wait_ms, alt, reset);
    fflush(stdout);
    h = wait_open(ctx);
    t0 = now();
    LOG("opened");
    libusb_set_auto_detach_kernel_driver(h, 1); /* Linux: take interface 0 from xpad; no-op on macOS */

    int cfg = -1;
    LOG("GET_CONFIGURATION -> %s, config=%d", libusb_error_name(libusb_get_configuration(h, &cfg)), cfg);
    uint8_t st[2] = {0};
    int r = libusb_control_transfer(h, 0x80, 0x00, 0, 0, st, 2, 500);
    LOG("GET_STATUS(device) -> %d, %02x %02x", r, st[0], st[1]);

    if (wait_ms) { LOG("leaving it alone for %d ms (watch the LED)", wait_ms); usleep(wait_ms * 1000); }

    if (reset) {
        LOG("reset -> %s", libusb_error_name(libusb_reset_device(h)));
        libusb_close(h);
        h = wait_open(ctx);
        LOG("reopened after reset");
    }

    if (enumr) replay_linux_enum();
    if (norwu) {
        // A freshly reset device must report remote wakeup off; this one reads 02 00 (on) before we touch it.
        int rc = libusb_control_transfer(h, 0x00, 0x01, 0x0001, 0, NULL, 0, 500); // CLEAR_FEATURE(DEVICE_REMOTE_WAKEUP)
        uint8_t s2[2] = {0};
        int r2 = libusb_control_transfer(h, 0x80, 0x00, 0, 0, s2, 2, 500);
        LOG("CLEAR_FEATURE(REMOTE_WAKEUP) -> %s; GET_STATUS now %d, %02x %02x", rc >= 0 ? "ok" : libusb_error_name(rc), r2, s2[0], s2[1]);
    }
    if (cfg != 1) {
        int rc;
        while ((rc = libusb_set_configuration(h, 1)) == LIBUSB_ERROR_NO_DEVICE) {
            LOG("set_configuration(1) -> NO_DEVICE (plug bounce); waiting for it to come back");
            libusb_close(h);
            usleep(20000);
            h = wait_open(ctx);
            LOG("reopened");
            libusb_set_auto_detach_kernel_driver(h, 1);
        }
        LOG("set_configuration(1) -> %s", libusb_error_name(rc));
    }
    LOG("claim 0 -> %s", libusb_error_name(libusb_claim_interface(h, 0)));
    if (alt) {
        LOG("claim 1 -> %s", libusb_error_name(libusb_claim_interface(h, 1)));
        LOG("SET_INTERFACE(1,0) -> %s", libusb_error_name(libusb_set_interface_alt_setting(h, 1, 0)));
    }

    if (clear) {
        // libusb_clear_halt sends CLEAR_FEATURE(ENDPOINT_HALT) AND resets the host-side toggle, so both ends restart at DATA0.
        LOG("clear_halt 0x02 -> %s", libusb_error_name(libusb_clear_halt(h, 0x02)));
        LOG("clear_halt 0x81 -> %s", libusb_error_name(libusb_clear_halt(h, 0x81)));
    }

    // Queue the IN transfer before the first OUT, like xpad does on open.
    struct libusb_transfer *t = libusb_alloc_transfer(0);
    static uint8_t buf[64];
    libusb_fill_interrupt_transfer(t, h, 0x81, buf, sizeof buf, on_in, NULL, 0);
    LOG("submit IN -> %s", libusb_error_name(libusb_submit_transfer(t)));

    static const uint8_t power_on[] = {0x05, 0x20, 0x00, 0x01, 0x00};
    static const uint8_t led_on[] = {0x0a, 0x20, 0x01, 0x03, 0x00, 0x01, 0x14};
    static const uint8_t auth[] = {0x06, 0x20, 0x02, 0x02, 0x01, 0x00};
    if (dup) sendpkt("POWER ON (sacrificial)", power_on, sizeof power_on);
    sendpkt("POWER ON", power_on, sizeof power_on);
    sendpkt("LED ON", led_on, sizeof led_on);
    sendpkt("AUTH DONE", auth, sizeof auth);

    for (int ep = 0; ep < 2; ep++) {
        uint8_t addr = ep ? 0x02 : 0x81, es[2] = {0};
        int rr = libusb_control_transfer(h, 0x82, 0x00, 0, addr, es, 2, 500);
        LOG("GET_STATUS(endpoint %#04x) -> %d, %02x %02x%s", addr, rr, es[0], es[1], (rr == 2 && (es[0] & 1)) ? "  HALTED" : "");
    }
    if (rumble) {
        // xpad's Xbox One rumble packet (GIP 0x09), both motors at 50% for 1 s, then off.
        uint8_t on[] = {0x09, 0x00, 0x03, 0x09, 0x00, 0x0f, 0x00, 0x00, 0x80, 0x80, 0x64, 0x00, 0x00};
        uint8_t off[] = {0x09, 0x00, 0x04, 0x09, 0x00, 0x0f, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
        sendpkt("RUMBLE ON", on, sizeof on);
        struct timeval tv1 = {0, 100000};
        for (int i = 0; i < 12; i++) libusb_handle_events_timeout(ctx, &tv1);
        sendpkt("RUMBLE OFF", off, sizeof off);
    }
    LOG("listening %d s: press buttons and move sticks", seconds);
    double end = now() + seconds;
    struct timeval tv = {0, 50000};
    while (now() < end && !done) libusb_handle_events_timeout(ctx, &tv);
    done = 1;
    libusb_cancel_transfer(t);
    for (int i = 0; i < 10; i++) libusb_handle_events_timeout(ctx, &tv);
    LOG("RESULT: %d IN packets, %d input reports (0x20)", total, inputs);
    libusb_close(h);
    libusb_exit(ctx);
    return 0;
}
