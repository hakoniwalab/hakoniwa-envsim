<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
  xmlns:gml="http://www.opengis.net/gml"
  xmlns:bldg="http://www.opengis.net/citygml/building/2.0"
  xmlns:app="http://www.opengis.net/citygml/appearance/2.0">
  <gml:boundedBy><gml:Envelope srsName="http://www.opengis.net/def/crs/EPSG/0/6697" srsDimension="3">
    <gml:lowerCorner>35.681200 139.706720 10</gml:lowerCorner>
    <gml:upperCorner>35.681250 139.706790 20</gml:upperCorner>
  </gml:Envelope></gml:boundedBy>
  <app:appearanceMember><app:Appearance><app:theme>rgbTexture</app:theme>
    <app:surfaceDataMember><app:ParameterizedTexture>
      <app:imageURI>tiny_mixed_lod_6697_appearance/roof.jpg</app:imageURI>
      <app:mimeType>image/jpeg</app:mimeType>
      <app:target uri="#textured-roof"><app:TexCoordList>
        <app:textureCoordinates ring="#textured-roof-ring">0 0 1 0 1 1 0 1 0 0</app:textureCoordinates>
      </app:TexCoordList></app:target>
    </app:ParameterizedTexture></app:surfaceDataMember>
  </app:Appearance></app:appearanceMember>
  <core:cityObjectMember><bldg:Building gml:id="textured-building">
    <bldg:lod1Solid><gml:Solid><gml:exterior><gml:CompositeSurface>
      <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>35.681200 139.706720 10 35.681200 139.706740 10 35.681220 139.706740 10 35.681220 139.706720 10 35.681200 139.706720 10</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
      <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>35.681200 139.706720 20 35.681220 139.706720 20 35.681220 139.706740 20 35.681200 139.706740 20 35.681200 139.706720 20</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
    </gml:CompositeSurface></gml:exterior></gml:Solid></bldg:lod1Solid>
    <bldg:boundedBy><bldg:RoofSurface><bldg:lod2MultiSurface><gml:MultiSurface><gml:surfaceMember>
      <gml:Polygon gml:id="textured-roof"><gml:exterior><gml:LinearRing gml:id="textured-roof-ring"><gml:posList>35.681200 139.706720 20 35.681220 139.706720 20 35.681220 139.706740 20 35.681200 139.706740 20 35.681200 139.706720 20</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>
    </gml:surfaceMember></gml:MultiSurface></bldg:lod2MultiSurface></bldg:RoofSurface></bldg:boundedBy>
  </bldg:Building></core:cityObjectMember>
  <core:cityObjectMember><bldg:Building gml:id="fallback-building">
    <bldg:lod1Solid><gml:Solid><gml:exterior><gml:CompositeSurface>
      <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>35.681230 139.706760 10 35.681230 139.706780 10 35.681250 139.706780 10 35.681250 139.706760 10 35.681230 139.706760 10</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
      <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>35.681230 139.706760 18 35.681250 139.706760 18 35.681250 139.706780 18 35.681230 139.706780 18 35.681230 139.706760 18</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
    </gml:CompositeSurface></gml:exterior></gml:Solid></bldg:lod1Solid>
  </bldg:Building></core:cityObjectMember>
</core:CityModel>
