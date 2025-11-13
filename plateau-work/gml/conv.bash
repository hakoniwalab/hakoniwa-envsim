#!/bin/bash

# Conv GML files in INPUT_DIRECTORY to an index JSON file named HEADING_NAME_index.json
# Usage: ./conv.bash <input_directory> <heading_name>
set -e

if [ $# -ne 6 ]; then
    echo "Usage: $0 <input_directory> <heading_name> <lat> <lon> <ns_meters> <ew_meters>"
    exit 1
fi

INPUT_DIR=$1
HEADING_NAME=$2
LAT=$3
LON=$4
NS_METERS=$5
EW_METERS=$6

#python gml_indexer.py  $INPUT_DIR  -o ${HEADING_NAME}_index.json

#python gml_extract.py  --src-root $INPUT_DIR --lat $LAT --lon $LON --ns $NS_METERS --ew $EW_METERS --out-root ${HEADING_NAME}_extracted --index ${HEADING_NAME}_index.json

#python gml_lod1_extract.py --in ${HEADING_NAME}_extracted --out ${HEADING_NAME}_lod1.json --to-epsg 6677

#python gml2obb.py --in ${HEADING_NAME}_lod1.json --out ${HEADING_NAME}_obb.json

python obb2mjcf.py --inp ${HEADING_NAME}_obb.json --out ${HEADING_NAME}.xml
