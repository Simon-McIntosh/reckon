#!/usr/bin/env bash
# Rasterise a figure so the session can look at it, optionally with zoom crops.
#
#   bash look.sh <figure.svg|png> [out-dir] [WxH+X+Y ...]
#
# Writes <out-dir>/<stem>.png at 1400 px wide (the 14 in canvas at 100 dpi) and
# one <stem>-crop<N>.png per crop geometry, then prints each path. out-dir
# defaults to $CLAUDE_SCRATCH, then $TMPDIR, then /tmp. The first renderer
# found is used: rsvg-convert (login nodes), ImageMagick convert (compute
# nodes), then cairosvg. Read the printed PNG; the SVG itself is not a look.
set -euo pipefail

src=${1:?usage: look.sh <figure.svg|png> [out-dir] [WxH+X+Y ...]}
out=${2:-${CLAUDE_SCRATCH:-${TMPDIR:-/tmp}}}
shift $(( $# >= 2 ? 2 : 1 ))
[ -f "$src" ] || { echo "look.sh: no such figure: $src" >&2; exit 2; }
mkdir -p "$out"
stem=$(basename "${src%.*}")
png="$out/$stem.png"

case "$src" in
  *.svg)
    if command -v rsvg-convert >/dev/null; then
      rsvg-convert -w 1400 -b white "$src" -o "$png"
    elif command -v convert >/dev/null; then
      # 72 dpi is one SVG point per pixel; the 14 in (1008 pt) canvas lands at
      # 1400 px when rendered at 100 dpi.
      convert -density 100 -background white -flatten "$src" "$png"
    elif python3 -c "import cairosvg" 2>/dev/null; then
      python3 -c "import cairosvg,sys; cairosvg.svg2png(url=sys.argv[1], write_to=sys.argv[2], output_width=1400, background_color='white')" "$src" "$png"
    else
      echo "look.sh: no SVG renderer (rsvg-convert, convert or cairosvg)" >&2; exit 3
    fi ;;
  *) cp "$src" "$png" ;;
esac
echo "$png"

n=0
for geometry in "$@"; do
  n=$((n + 1))
  crop="$out/$stem-crop$n.png"
  convert "$png" -crop "$geometry" +repage "$crop"
  echo "$crop"
done
