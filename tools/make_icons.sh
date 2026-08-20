#!/bin/sh
# Regenerate the raster icons from the SVG sources in web/static/icons.
# Needs librsvg (rsvg-convert) and ImageMagick. Run it after editing an SVG
# and commit the results -- the Pi never rasterises anything.
#
# icon.svg is the master and holds up from 16 px to 512 px, so every raster
# below comes from it; icon-maskable.svg is the same mark inset into the safe
# zone Android crops to.
set -eu
cd "$(dirname "$0")/../vinyl_archive/web/static/icons"

rsvg-convert -w 180 -h 180 icon.svg          -o apple-touch-icon.png
rsvg-convert -w 192 -h 192 icon.svg          -o icon-192.png
rsvg-convert -w 512 -h 512 icon.svg          -o icon-512.png
rsvg-convert -w 512 -h 512 icon-maskable.svg -o icon-maskable-512.png

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for size in 16 32 48; do
    rsvg-convert -w "$size" -h "$size" icon.svg -o "$tmp/$size.png"
done
magick "$tmp/16.png" "$tmp/32.png" "$tmp/48.png" favicon.ico

echo "icons regenerated"
