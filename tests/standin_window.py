"""A stand-in for the game's window, for the tests of grfs.desktop and tools/rig.py: a known
picture - red left half, blue right half - and a record of every key and click it gets.

    python tests/standin_window.py RECORD_FILE [TITLE]

Each line of the record is 'key <keysym>' or 'click <button> <x> <y>'. It closes itself after
two minutes in case a test forgets it.
"""

import sys
import tkinter


def main():
    record, title = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "GRFS stand-in")
    root = tkinter.Tk()
    root.title(title)
    root.geometry("320x200+40+40")
    root.resizable(False, False)
    canvas = tkinter.Canvas(root, width=320, height=200, highlightthickness=0, bd=0)
    canvas.pack()
    canvas.create_rectangle(0, 0, 160, 200, fill="#ff0000", width=0)
    canvas.create_rectangle(160, 0, 320, 200, fill="#0000ff", width=0)

    def note(line):
        with open(record, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    # The key's hardware code as well as its symbol: with a Russian layout the key A types a
    # Cyrillic letter, and the symbol says nothing useful about which key it was.
    root.bind("<Key>", lambda e: note("key %s %d %s" % (e.keysym.encode("ascii", "replace")
                                                         .decode(), e.keycode,
                                                         ascii(e.char))))
    # Logical pixels: the window is not DPI-aware, so on a scaled screen these are the
    # screenshot's pixels divided by the scale.
    canvas.bind("<Button>", lambda e: note("click %d %d %d %g" % (
        e.num, e.x, e.y, root.winfo_fpixels("1i") / 96)))
    root.after(120000, root.destroy)
    root.after(200, lambda: note("ready"))
    root.mainloop()


if __name__ == "__main__":
    main()
