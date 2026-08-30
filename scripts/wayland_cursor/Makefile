# SPDX-License-Identifier: Apache-2.0
# Builds the minimal Wayland test client for the cursor-hiding prototype.

CC      ?= gcc
PKG     := pkg-config
CFLAGS  += -Wall -Wextra -O2 $(shell $(PKG) --cflags wayland-client)
LDLIBS  += $(shell $(PKG) --libs wayland-client)

# xdg-shell protocol XML (from wayland-protocols)
XDG_SHELL_XML := /usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml

# Generated protocol code
GEN_C := xdg-shell-protocol.c
GEN_H := xdg-shell-client-protocol.h

all: client

# Generate the xdg-shell client protocol code.
$(GEN_C) $(GEN_H): $(XDG_SHELL_XML)
	wayland-scanner client-header $(XDG_SHELL_XML) $(GEN_H)
	wayland-scanner private-code $(XDG_SHELL_XML) $(GEN_C)

client: client.c $(GEN_C) $(GEN_H)
	$(CC) $(CFLAGS) -o $@ client.c $(GEN_C) $(LDLIBS)

clean:
	rm -f client $(GEN_C) $(GEN_H)

.PHONY: all clean