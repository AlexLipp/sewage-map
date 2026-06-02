
# Mapping Sewage Discharges into rivers across England <!--and Wales-->

![opengraphsocial](https://github.com/user-attachments/assets/45473ed6-309c-419a-b8fe-f70575043b2b)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)


Realtime mapping the downstream impact of Combined Sewage Overflow discharge events in non-tidal rivers across England and Scotland <!--and Wales-->. This repository provides the back-end for [`www.sewagemap.co.uk`](https://www.sewagemap.co.uk/). The repository  for the front-end is available at [`github.com/JonnyDawe/UK-Sewage-Map/`](https://github.com/JonnyDawe/UK-Sewage-Map/).

This was developed by [Alex Lipp](https://alexlipp.github.io/), [Jonny Dawe](https://www.linkedin.com/in/jonathan-dawe-46180212a) and Sudhir Balaji. Please feel free to raise an issue above or contact us directly.

[![Twitter Follow](https://img.shields.io/twitter/follow/alexglipp?style=social)](https://twitter.com/intent/follow?screen_name=AlexGLipp)
[![Twitter Follow](https://img.shields.io/twitter/follow/JdMapDev?style=social)](https://twitter.com/intent/follow?screen_name=JdMapDev)
[![GitHub followers](https://img.shields.io/github/followers/AlexLipp?label=AlexLipp&style=social)](https://github.com/AlexLipp)
[![GitHub followers](https://img.shields.io/github/followers/JonnyDawe?label=JonnyDawe&style=social)](https://github.com/JonnyDawe)
[![GitHub followers](https://img.shields.io/github/followers/sudhir-b?label=sudhir-b&style=social)](https://github.com/sudhir-b)

- [Installation](#installation)
- [Usage](#usage)
- [Output Data](#output-data)
   - [Water Company Data Table](#water-company-data-table)
- [Source Data](#source-data)

## Installation

This script relies on the `POOPy` package which I ([Alex](https://alexlipp.github.io/)) have created to allow easy interaction with Water Company Event Duration Monitoring (EDM) APIs, and analysis of the data. This is freely available at: [`github.com/AlexLipp/POOPy`](https://github.com/AlexLipp/POOPy).

## Usage

The core script is `update_all.py` which is called automatically every 15 minutes. This function, using POOPy functions, calculates a geoJSON file which contains the downstream impact of all active or recently active CSO spills for which EDM data is available. These are automatically uploaded to the Amazon Web Services bucket which hosts them. The data is then fronted using the CloudFront delivery service. The files are read by the `www.sewagemap.co.uk` front-end which visualises them. 

The script `update.py` is included for legacy purposes, and updates only Thames Water data (but is needed for the historical data provided only by Thames).

## Source data 

The live EDM data used to map downstream sections is sourced as follows:

- In England, from the [Stream Storm Overflow Data Hub](https://www.streamwaterdata.co.uk/pages/storm-overflows-data), which is provided under a CC-BY license.
- For Thames Water, from the [Thames Water API](https://data.thameswater.co.uk/s/).
- In Scotland, from the [Scottish Water API](https://www.scottishwater.co.uk/Help-and-Resources/Open-Data/Overflow-Map-Data).

<!-- For Wales, we use data presented on the [WelshWater Storm Overflow map](https://corporate.dwrcymru.com/en/community/environment/storm-overflow-map).-->
