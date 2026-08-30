/*
 * SPDX-License-Identifier: Apache-2.0
 * Minimal Wayland test client for the cursor-hiding prototype.
 *
 * Opens a fullscreen solid-colour surface so we can see whether the
 * compositor (cage) draws a cursor over it. If the cursor is hidden,
 * only the colour is visible.
 *
 * Build:  make
 * Run:    (inside cage) ./client
 */

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include <wayland-client.h>

/* --- xdg-shell protocol (generated) --- */
#include "xdg-shell-client-protocol.h"

#define WIDTH 64
#define HEIGHT 64

static struct wl_display *display;
static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct wl_surface *surface;
static struct xdg_wm_base *xdg_wm_base;
static struct xdg_surface *xdg_surface;
static struct xdg_toplevel *xdg_toplevel;

static int running = 1;

/* --- xdg_wm_base listeners --- */

static void
xdg_wm_base_ping(void *data, struct xdg_wm_base *wm_base, uint32_t serial)
{
    xdg_wm_base_pong(wm_base, serial);
}

static const struct xdg_wm_base_listener xdg_wm_base_listener = {
    .ping = xdg_wm_base_ping,
};

/* --- xdg_surface listeners --- */

static void
xdg_surface_configure(void *data, struct xdg_surface *s, uint32_t serial)
{
    xdg_surface_ack_configure(s, serial);
}

static const struct xdg_surface_listener xdg_surface_listener = {
    .configure = xdg_surface_configure,
};

/* --- xdg_toplevel listeners --- */

static void
xdg_toplevel_configure(void *data, struct xdg_toplevel *t, int32_t w, int32_t h,
                       struct wl_array *states)
{
    /* Ignore — cage maximises/fullscreens us. */
}

static void
xdg_toplevel_close(void *data, struct xdg_toplevel *t)
{
    running = 0;
}

static const struct xdg_toplevel_listener xdg_toplevel_listener = {
    .configure = xdg_toplevel_configure,
    .close = xdg_toplevel_close,
};

/* --- shm helpers --- */

static int
create_shm_file(size_t size)
{
    char name[] = "/dev/shm/metixel-cursor-XXXXXX";
    int fd = mkstemp(name);
    if (fd < 0)
        return -1;
    if (ftruncate(fd, (off_t)size) < 0) {
        close(fd);
        return -1;
    }
    unlink(name);
    return fd;
}

static struct wl_buffer *
create_buffer(void)
{
    int stride = WIDTH * 4;
    int size = stride * HEIGHT;
    int fd = create_shm_file(size);
    if (fd < 0) {
        fprintf(stderr, "client: create_shm_file failed\n");
        return NULL;
    }

    void *data = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (data == MAP_FAILED) {
        fprintf(stderr, "client: mmap failed\n");
        close(fd);
        return NULL;
    }

    /* Solid dark blue (BGRA). */
    uint32_t *pixels = data;
    for (int i = 0; i < WIDTH * HEIGHT; i++) {
        pixels[i] = 0xFF204060; /* B=0x60 G=0x40 R=0x20 A=0xFF */
    }

    if (!shm) {
        fprintf(stderr, "client: wl_shm not bound\n");
        munmap(data, size);
        close(fd);
        return NULL;
    }

    struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
    if (!pool) {
        fprintf(stderr, "client: wl_shm_create_pool failed\n");
        munmap(data, size);
        close(fd);
        return NULL;
    }
    struct wl_buffer *buffer =
        wl_shm_pool_create_buffer(pool, 0, WIDTH, HEIGHT, stride, WL_SHM_FORMAT_XRGB8888);
    wl_shm_pool_destroy(pool);
    munmap(data, size);
    close(fd);
    return buffer;
}

/* --- registry --- */

static void
registry_global(void *data, struct wl_registry *registry, uint32_t name,
                const char *interface, uint32_t version)
{
    if (strcmp(interface, wl_compositor_interface.name) == 0) {
        compositor = wl_registry_bind(registry, name, &wl_compositor_interface, 4);
    } else if (strcmp(interface, wl_shm_interface.name) == 0) {
        shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
    } else if (strcmp(interface, xdg_wm_base_interface.name) == 0) {
        xdg_wm_base = wl_registry_bind(registry, name, &xdg_wm_base_interface, 1);
        xdg_wm_base_add_listener(xdg_wm_base, &xdg_wm_base_listener, NULL);
    }
}

static void
registry_global_remove(void *data, struct wl_registry *registry, uint32_t name)
{
    /* no-op */
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

int
main(void)
{
    display = wl_display_connect(NULL);
    if (!display) {
        fprintf(stderr, "client: cannot connect to Wayland display\n");
        return 1;
    }

    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    wl_display_roundtrip(display);

    if (!compositor || !shm || !xdg_wm_base) {
        fprintf(stderr, "client: missing compositor, shm, or xdg_wm_base\n");
        return 1;
    }

    surface = wl_compositor_create_surface(compositor);
    xdg_surface = xdg_wm_base_get_xdg_surface(xdg_wm_base, surface);
    xdg_surface_add_listener(xdg_surface, &xdg_surface_listener, NULL);

    xdg_toplevel = xdg_surface_get_toplevel(xdg_surface);
    xdg_toplevel_add_listener(xdg_toplevel, &xdg_toplevel_listener, NULL);
    xdg_toplevel_set_title(xdg_toplevel, "metixel-cursor-test");
    xdg_toplevel_set_app_id(xdg_toplevel, "metixel-cursor-test");

    wl_surface_commit(surface);
    wl_display_roundtrip(display);

    /* Request fullscreen. */
    xdg_toplevel_set_fullscreen(xdg_toplevel, NULL);

    /* Attach a solid-colour buffer. */
    struct wl_buffer *buffer = create_buffer();
    if (!buffer) {
        fprintf(stderr, "client: failed to create buffer\n");
        return 1;
    }
    wl_surface_attach(surface, buffer, 0, 0);
    wl_surface_damage_buffer(surface, 0, 0, WIDTH, HEIGHT);
    wl_surface_commit(surface);
    wl_display_roundtrip(display);

    fprintf(stderr, "client: running (cursor test). Press Ctrl+C to exit.\n");

    while (running && wl_display_dispatch(display) != -1) {
        /* loop */
    }

    wl_display_disconnect(display);
    return 0;
}