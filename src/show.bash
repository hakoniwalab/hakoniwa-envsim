#!/bin/bash

python hakoniwa_envsim/visualizer/plot2d.py \
  --area ../examples/datasets/kobe/generated/area.json \
  --property ../examples/datasets/kobe/generated/property.json \
  --link ../examples/datasets/kobe/generated/link.json \
  --overlay-map --origin-lat 34.65436065 --origin-lon 135.17003369 --offset-x 0 --offset-y 0 \
  --print-shifted-origin
