
# Mapping Sewage Discharges into rivers across England <!--and Wales-->

![opengraphsocial](https://github.com/user-attachments/assets/45473ed6-309c-419a-b8fe-f70575043b2b)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)


Realtime mapping the downstream impact of Combined Sewage Overflow discharge events in non-tidal rivers across England <!--and Wales-->. This repository provides the back-end for [`www.sewagemap.co.uk`](https://www.sewagemap.co.uk/). The repository  for the front-end is available at [`github.com/JonnyDawe/UK-Sewage-Map/`](https://github.com/JonnyDawe/UK-Sewage-Map/).

This was developed by [Alex Lipp](https://alexlipp.github.io/), [Jonny Dawe](https://www.linkedin.com/in/jonathan-dawe-46180212a) and Sudhir Balaji. Please feel free to raise an issue above or contact us directly.

[![Twitter Follow](https://img.shields.io/twitter/follow/alexglipp?style=social)](https://twitter.com/intent/follow?screen_name=AlexGLipp)
[![Twitter Follow](https://img.shields.io/twitter/follow/JdMapDev?style=social)](https://twitter.com/intent/follow?screen_name=JdMapDev)
[![GitHub followers](https://img.shields.io/github/followers/AlexLipp?label=AlexLipp&style=social)](https://github.com/AlexLipp)
[![GitHub followers](https://img.shields.io/github/followers/JonnyDawe?label=JonnyDawe&style=social)](https://github.com/JonnyDawe)
[![GitHub followers](https://img.shields.io/github/followers/sudhir-b?label=sudhir-b&style=social)](https://github.com/sudhir-b)

## Installation

This script relies on the `POOPy` package which I ([Alex](https://alexlipp.github.io/)) have created to allow easy interaction with Water Company Event Duration Monitoring (EDM) APIs, and analysis of the data. This is freely available at: [`github.com/AlexLipp/POOPy`](https://github.com/AlexLipp/POOPy).

## Usage

The core script is `update_all.py` which is called automatically every 15 minutes. This function, using POOPy functions, calculates a geoJSON file which contains the downstream impact of all active or recently active CSO spills for which EDM data is available. These are automatically uploaded to the Amazon Web Services bucket which hosts them. The data is then fronted using the CloudFront delivery service. The files are read by the `www.sewagemap.co.uk` front-end which visualises them. 

The script `update.py` is included for legacy purposes, and updates only Thames Water data (but is needed for the historical data provided only by Thames).

## Output data
 [![License:CC BY NC SA](https://licensebuttons.net/l/by-nc-sa/4.0/88x31.png)](https://creativecommons.org/licenses/by-nc-sa/4.0/)

The live downstream impact of Combined Sewage Overflow (CSO) discharge events **is freely available** under a [CC-BY-NC-SA](https://creativecommons.org/licenses/by-nc-sa/4.0/) license. The links in the table below give access to the data as `.geoJSON` files. The data are updated automatically every ~20 minutes (but the URL remains the same). These can be incorporated into your own projects, web-apps, or GIS projects but please attribute the source as `www.sewagemap.co.uk`. It'd be wonderful to hear about any projects you use this data in, so please do [reach out to me](https://alexlipp.github.io/) to let me know, or if I can be of any assistance.

The _Downstream impacted reaches_ is a `LineString` feature-collection simply showing the sections of a river which are downstream of current discharges, and optionally those in the last 48 hrs. These are the brown lines on `www.sewagemap.co.uk`. 

The _Downstream Impact Information_ is a `Point` feature-collection which details at each pixel in a drainage network 1) the number of discharges upstream, 2) the number of discharges per unit of upstream area, and 3) A list of the names (or permit numbers) of discharging CSOs upstream.  

 **Disclaimer: whilst we make every effort to ensure the accuracy of this data, we cannot guarantee it and it should not be used for any critical purposes.**  

Water Company | Downstream Impacted Reaches | Downstream Impact Information | Last Updated 
--- | --- | --- | ---
Thames Water | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/thames/thames_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/thames/thames_now_incl_48hrs.geojson) | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/thames/thames_info_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/thames/thames_info_now_incl_48hrs.geojson) | [Timestamp](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/thames/thames_timestamp.txt)
Anglian Water | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/anglian/anglian_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/anglian/anglian_now_incl_48hrs.geojson) | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/anglian/anglian_info_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/anglian/anglian_info_now_incl_48hrs.geojson) | [Timestamp](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/anglian/anglian_timestamp.txt)
United Utilities | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/united/united_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/united/united_now_incl_48hrs.geojson) | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/united/united_info_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/united/united_info_now_incl_48hrs.geojson) | [Timestamp](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/united/united_timestamp.txt)
Southern Water | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/southern/southern_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/southern/southern_now_incl_48hrs.geojson) | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/southern/southern_info_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/southern/southern_info_now_incl_48hrs.geojson) | [Timestamp](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/southern/southern_timestamp.txt)
Northumbrian Water | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/northumbrian/northumbrian_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/northumbrian/northumbrian_now_incl_48hrs.geojson) | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/northumbrian/northumbrian_info_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/northumbrian/northumbrian_info_now_incl_48hrs.geojson) | [Timestamp](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/northumbrian/northumbrian_timestamp.txt)
Severn Trent Water | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/severntrent/severntrent_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/severntrent/severntrent_now_incl_48hrs.geojson) | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/severntrent/severntrent_info_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/severntrent/severntrent_info_now_incl_48hrs.geojson) | [Timestamp](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/severntrent/severntrent_timestamp.txt)
Wessex Water | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/wessex/wessex_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/wessex/wessex_now_incl_48hrs.geojson) | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/wessex/wessex_info_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/wessex/wessex_info_now_incl_48hrs.geojson) | [Timestamp](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/wessex/wessex_timestamp.txt)
Yorkshire Water | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/yorkshire/yorkshire_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/yorkshire/yorkshire_now_incl_48hrs.geojson) | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/yorkshire/yorkshire_info_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/yorkshire/yorkshire_info_now_incl_48hrs.geojson) | [Timestamp](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/yorkshire/yorkshire_timestamp.txt)
SouthWest Water | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/southwest/southwest_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/southwest/southwest_now_incl_48hrs.geojson) | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/southwest/southwest_info_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/southwest/southwest_info_now_incl_48hrs.geojson) | [Timestamp](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/southwest/southwest_timestamp.txt)
 <!--Welsh Water | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/welsh/welsh_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/welsh/welsh_now_incl_48hrs.geojson) | [Current spills](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/welsh/welsh_info_now_excl_48hrs.geojson); [Spills within last 48hrs](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/welsh/welsh_info_now_incl_48hrs.geojson) | [Timestamp](https://d1kmd884co9q6x.cloudfront.net/downstream_impact/welsh/welsh_timestamp.txt)-->
## Source data 

The live EDM data which we use to map downstream sectons is sourced, in England, from the [Stream Storm Overflow Data Hub](https://www.streamwaterdata.co.uk/pages/storm-overflows-data) which is provided under a CC-BY license. Each individual water company maintain's an individual API which you can access from the given link. <!-- For Wales, we use data presented on the [WelshWater Storm Overflow map](https://corporate.dwrcymru.com/en/community/environment/storm-overflow-map).-->
