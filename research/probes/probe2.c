// Linux-order probe: claim both interfaces, select alt 0 on interface 1,
// then POWER ON / LED / AUTH back to back with no pauses, then listen.
#include <libusb.h>
#include <stdio.h>
#include <stdint.h>
#include <unistd.h>

static libusb_device_handle *h;

static void send(const char *name, uint8_t *pkt, int len) {
    int n = 0;
    int r = libusb_interrupt_transfer(h, 0x02, pkt, len, &n, 1000);
    printf("  %s -> %s (%d bytes)\n", name, libusb_error_name(r), n);
}

int main(void) {
    libusb_context *ctx;
    libusb_init(&ctx);
    printf("Waiting for 2dc8:2027 (plug it in now)...\n");
    while ((h = libusb_open_device_with_vid_pid(ctx, 0x2dc8, 0x2027)) == NULL) usleep(20000);
    printf("opened\n");

    printf("  claim 0 -> %s\n", libusb_error_name(libusb_claim_interface(h, 0)));
    printf("  claim 1 -> %s\n", libusb_error_name(libusb_claim_interface(h, 1)));
    printf("  set_interface_alt(1,0) -> %s\n", libusb_error_name(libusb_set_interface_alt_setting(h, 1, 0)));

    uint8_t power_on[]  = {0x05, 0x20, 0x00, 0x01, 0x00};
    uint8_t led_on[]    = {0x0a, 0x20, 0x01, 0x03, 0x00, 0x01, 0x14};
    uint8_t auth_done[] = {0x06, 0x20, 0x02, 0x02, 0x01, 0x00};
    send("POWER ON", power_on, sizeof(power_on));
    send("LED ON", led_on, sizeof(led_on));
    send("AUTH DONE", auth_done, sizeof(auth_done));

    uint8_t buf[64]; int n, shown = 0, total = 0;
    for (int ms = 0; ms < 8000; ms += 20) {
        int r = libusb_interrupt_transfer(h, 0x81, buf, sizeof(buf), &n, 20);
        if (r == 0) {
            total++;
            if (shown++ < 6) {
                printf("  recv %d bytes:", n);
                for (int i = 0; i < n && i < 12; i++) printf(" %02x", buf[i]);
                printf("\n");
            }
        } else if (r != LIBUSB_ERROR_TIMEOUT) { printf("  read error: %s\n", libusb_error_name(r)); break; }
    }
    printf("received %d packets in 8s (Linux gets ~2000)\n", total);
    libusb_close(h); libusb_exit(ctx);
    return 0;
}
