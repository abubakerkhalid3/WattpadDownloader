
{ pkgs }: {
  deps = [
    pkgs.calibre
    pkgs.exiftool
    pkgs.pango
    pkgs.harfbuzz
    pkgs.fribidi
    pkgs.glib
    pkgs.fontconfig
    pkgs.freetype
    pkgs.cairo
    pkgs.gdk-pixbuf
    pkgs.gobject-introspection
  ];
}
