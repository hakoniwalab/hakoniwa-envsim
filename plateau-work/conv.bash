#!/bin/bash

if [ $# -ne 2 -a $# -ne 1 ]; then
    echo "Usage: $0 <input_file> [<mjcf_extract_num>]"
    exit 1
fi
input_file=$1
base_name=$(basename "$input_file" .gml)
if [ $# -eq 1 ]; then
    mjcf_extract_num=-1
else
    mjcf_extract_num=$2
fi

python gml_lod1_extract.py \
    --in $input_file \
    --out="extracted_${base_name}.json" \
    --to-epsg 6677

python rotating_calipers.py --in "extracted_${base_name}.json" \
    --out "extracted_${base_name}_lod1_obb.json" --mode after \
    --no-legend --no-show

echo "[INFO] MJCF conversion with mjcf_extract_num=${mjcf_extract_num}"
if [ ${mjcf_extract_num} -le 0 ]
then
    echo "[INFO] mjcf_extract_num is <= 0, skipping MJCF conversion."
    python obbjson2mjcf.py \
        --inp "extracted_${base_name}_lod1_obb.json" \
        --out "extracted_${base_name}.xml"
else
    python obbjson2mjcf.py \
        --inp "extracted_${base_name}_lod1_obb.json" \
        --out "extracted_${base_name}.xml" \
        --head $mjcf_extract_num
fi
