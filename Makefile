CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -Wpedantic -std=c11
LDLIBS += -lX11 -lXext -lXtst -lXfixes -lpng -ljpeg -lm -pthread

.PHONY: all clean

all: play-c recording-editor

play-c: play.c
	$(CC) $(CFLAGS) $(shell pkg-config --cflags x11 xext xtst xfixes libpng libjpeg) -o $@ $< $(LDLIBS)

recording-editor: recording_editor.c
	$(CC) $(CFLAGS) $(shell pkg-config --cflags gtk+-3.0) -o $@ $< $(shell pkg-config --libs gtk+-3.0) -lm

clean:
	rm -f play-c recording-editor
