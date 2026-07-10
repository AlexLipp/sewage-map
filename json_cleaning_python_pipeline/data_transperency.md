## Data Sources and Cleaning Pipeline

The standardised data used by our JSON cleaning scripts consists of Event Duration Monitoring (EDM) start-and-stop records from the following water companies:

* Anglian Water
* Northumbrian Water
* Severn Trent Water
* South West Water
* Southern Water
* United Utilities
* Wessex Water
* Yorkshire Water

The available data mainly covers the period from 2024 to 2026. We have prioritised these recent datasets because they contain the most up-to-date unique identifiers for Combined Sewer Overflow (CSO) monitoring sites. These identifiers allow us to match each EDM record to the corresponding CSO location using publicly available data from the StormHub ArcGIS APIs.

We have also requested additional historical data from before 2024. In these requests, we asked the water companies to provide the historical start-and-stop records using the current unique CSO identifiers. This would allow us to link older discharge events to the correct monitoring locations and display discharge durations across a longer period.

In addition, we have requested information on periods when CSO sensors were inactive or unavailable. Including these periods on the SewageMap website is important because gaps in recorded discharge activity may be caused by sensor downtime rather than an absence of overflow events.

## Water Company EDM Data Sources

**Anglian Water:**
https://www.anglianwater.co.uk/environment/storm-overflows/monthly-edm-publication

**Northumbrian Water:**
https://ckan.publishing.service.gov.uk/dataset/event-duration-monitoring-storm-overflow-start-stop-detailed-data

**Severn Trent Water:**
https://www.stwater.co.uk/get-river-positive/event-duration-monitor-edm-report-5/

**Southern Water:**
https://www.southernwater.co.uk/about-us/environmental-performance/healthy-rivers-and-seas/flow-and-spill-reporting/#flowdata

**South West Water:**
https://www.southwestwater.co.uk/environment/rivers-and-bathing-waters/waterfitlive/storm-overflow-map

**United Utilities:**
https://www.unitedutilities.com/better-rivers/our-challenges/storm-overflow-performance/

**Wessex Water:**
https://corporate.wessexwater.co.uk/our-purpose/rivers-and-coastal-waters/storm-overflows

**Yorkshire Water:**
https://www.yorkshirewater.com/environment/river-health/storm-overflow-investment/event-duration-monitoring/

## StormHub API Sources

The following StormHub ArcGIS API endpoints were used to obtain supplementary information about each CSO site, including its geographical location and receiving watercourse.

**Anglian Water:**
https://services3.arcgis.com/VCOY1atHWVcDlvlJ/arcgis/rest/services/stream_service_outfall_locations_view/FeatureServer/0/query

**Northumbrian Water:**
https://services-eu1.arcgis.com/MSNNjkZ51iVh8yBj/arcgis/rest/services/Northumbrian_Water_Storm_Overflow_Activity_2_view/FeatureServer/0/query

**Severn Trent Water:**
https://services1.arcgis.com/NO7lTIlnxRMMG9Gw/arcgis/rest/services/Severn_Trent_Water_Storm_Overflow_Activity/FeatureServer/0/query

**Southern Water:**
https://services-eu1.arcgis.com/6qJmARkS2dt2IjVA/arcgis/rest/services/SouthernWater_StormOverflowActivity_PROD_view/FeatureServer/0/query

**South West Water:**
https://services-eu1.arcgis.com/OMdMOtfhATJPcHe3/arcgis/rest/services/NEH_outlets_PROD/FeatureServer/0/query?outFields=*&where=1%3D1&f=geojson

**United Utilities:**
https://services5.arcgis.com/5eoLvR0f8HKb7HWP/arcgis/rest/services/United_Utilities_Storm_Overflow_Activity/FeatureServer/0/query

**Wessex Water:**
https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services/Wessex_Water_Storm_Overflow_Activity/FeatureServer/0/query

**Yorkshire Water:**
https://services-eu1.arcgis.com/1WqkK5cDKUbF0CkH/arcgis/rest/services/Yorkshire_Water_Storm_Overflow_Activity/FeatureServer/0/query

## Transparency and Data Processing

We designed the data pipeline to be transparent and traceable. The sources listed above show where the original EDM data came from, how CSO locations and receiving watercourses were obtained through the StormHub APIs, and how the information was cleaned and converted into a standardised JSON format.

The final JSON files are structured for use by the SewageMap website, allowing discharge events from different water companies and reporting formats to be presented consistently.
