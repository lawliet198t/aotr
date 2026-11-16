CC ?= cc
PKG_CONFIG ?= pkg-config

BUILD_DIR := build
INPUT_BIN := $(BUILD_DIR)/x11-input

CFLAGS += -std=c11 -O2 -Wall -Wextra -Wpedantic $(shell $(PKG_CONFIG) --cflags x11 xtst)
LDLIBS += $(shell $(PKG_CONFIG) --libs x11 xtst)

.PHONY: all clean
all: $(INPUT_BIN)

$(BUILD_DIR):
	mkdir -p "$@"

$(INPUT_BIN): x11_input.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) x11_input.c -o "$@" $(LDFLAGS) $(LDLIBS)

clean:
	rm -rf "$(BUILD_DIR)"
