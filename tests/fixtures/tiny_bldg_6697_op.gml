<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
  xmlns:gml="http://www.opengis.net/gml"
  xmlns:bldg="http://www.opengis.net/citygml/building/2.0">
  <gml:boundedBy><gml:Envelope srsName="http://www.opengis.net/def/crs/EPSG/0/6697" srsDimension="3">
    <gml:lowerCorner>35.68120 139.70672 10</gml:lowerCorner>
    <gml:upperCorner>35.68127 139.70681 20</gml:upperCorner>
  </gml:Envelope></gml:boundedBy>
  <core:cityObjectMember>
    <bldg:Building gml:id="tiny-building">
      <bldg:lod1Solid><gml:Solid><gml:exterior><gml:CompositeSurface>
        <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing>
          <gml:posList>35.68120 139.70672 10 35.68120 139.70681 10 35.68127 139.70681 10 35.68127 139.70672 10 35.68120 139.70672 10</gml:posList>
        </gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
        <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing>
          <gml:posList>35.68120 139.70672 20 35.68127 139.70672 20 35.68127 139.70681 20 35.68120 139.70681 20 35.68120 139.70672 20</gml:posList>
        </gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
      </gml:CompositeSurface></gml:exterior></gml:Solid></bldg:lod1Solid>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
