#!/usr/bin/python
from datetime import datetime
from geojson import Feature, FeatureCollection
import json
import os
import warnings

from aux_funcs import (
    empty_s3_folder,
    project_featurecollection_BNG_WGS84,
    upload_file_to_s3,
    write_timestamp,
)

from poopy.companies import (
    AnglianWater,
    NorthumbrianWater,
    SevernTrentWater,
    SouthWestWater,
    SouthernWater,
    ThamesWater,
    UnitedUtilities,
    WelshWater,
    WessexWater,
    YorkshireWater,
)

# Name of the whole bucket to upload to
BUCKET_NAME = "thamessewage"

# Name of the AWS profile to use (set as an environment variable)
PROFILE_NAME = os.getenv("S3_PROFILE_NAME")
if PROFILE_NAME is None:
    raise ValueError(
        "AWS profile name is missing from the environment!\n Please set it and try again."
    )

# Name of the geojson files in the AWS bucket for information on downstream impact of spills & recent spills (last 48hrs)
AWS_GEOJSON_FILENAME = "now_incl_48hrs.geojson"
# Name of the geojson files in the AWS bucket for information on downstream impact of current spills
AWS_GEOJSON_FILENAME_EXCL_48HRS = "now_excl_48hrs.geojson"
# Name of the info geojson files in the AWS bucket for information on downstream impact of spills & recent spills (last 48hrs)
AWS_INFO_GEOJSON_FILENAME = "info_now_incl_48hrs.geojson"
# Name of the info geojson files in the AWS bucket for information on downstream impact of current spills
AWS_INFO_GEOJSON_FILENAME_EXCL_48HRS = "info_now_excl_48hrs.geojson"

# Name of the timestamp files in the AWS bucket for information on downstream impact of current spills
TIMESTAMP_FILENAME = "timestamp.txt"

tw_clientID = os.getenv("TW_CLIENT_ID")
tw_clientSecret = os.getenv("TW_CLIENT_SECRET")
if tw_clientID is None or tw_clientSecret is None:
    raise ValueError(
        "Thames Water API keys are missing from the environment!\n Please set them and try again."
    )

# Local directory to save outputs to
LOCAL_OUTPUT_DIR = "output_dir/"
# Local directory to save geojsons to
LOCAL_GEOJSON_DIR = LOCAL_OUTPUT_DIR + "geojsons/"

watercompanies = [
    "thames",  # Thames Water
    "southern",  # Southern Water
    "anglian",  # Anglian Water
    "united",  # United Utilities
    "severntrent",  # Severn Trent Water
    "southwest",  # South West Water
    "yorkshire",  # Yorkshire Water
    "northumbrian",  # Northumbrian Water
    "wessex",  # Wessex Water
    "welsh",  # Welsh Water
]

# Create a nested dictionary to store info for each watercompany. Each watercompany will have a dictionary with the following keys:
# WaterCompany class (e.g. ThamesWater, WelshWater, etc.). local_output_dir (e.g. "output_dir/[name of water company]/"),
# AWS folder name (e.g. "downstream_impact/water_company/").

startime = datetime.now()

watercompany_info = {}
for company in watercompanies:
    watercompany_info[company] = {}
    watercompany_info[company]["local_output_dir"] = (
        LOCAL_OUTPUT_DIR + "downstream_impact/" + company + "/"
    )
    watercompany_info[company]["aws_folder_name"] = "downstream_impact/" + company + "/"
    if company == "thames":
        watercompany_info[company]["WaterCompany"] = ThamesWater(
            tw_clientID, tw_clientSecret
        )
    elif company == "welsh":
        watercompany_info[company]["WaterCompany"] = WelshWater()
    elif company == "southern":
        watercompany_info[company]["WaterCompany"] = SouthernWater()
    elif company == "anglian":
        watercompany_info[company]["WaterCompany"] = AnglianWater()
    elif company == "united":
        watercompany_info[company]["WaterCompany"] = UnitedUtilities()
    elif company == "severntrent":
        watercompany_info[company]["WaterCompany"] = SevernTrentWater()
    elif company == "southwest":
        watercompany_info[company]["WaterCompany"] = SouthWestWater()
    elif company == "yorkshire":
        watercompany_info[company]["WaterCompany"] = YorkshireWater()
    elif company == "northumbrian":
        watercompany_info[company]["WaterCompany"] = NorthumbrianWater()
    elif company == "wessex":
        watercompany_info[company]["WaterCompany"] = WessexWater()

# For each water company check that local output directory exists and create it if it doesn't
for company in watercompanies:
    if not os.path.exists(watercompany_info[company]["local_output_dir"]):
        print(f"Creating directory {watercompany_info[company]['local_output_dir']}")
        os.makedirs(watercompany_info[company]["local_output_dir"])


now = datetime.now()

# Now we loop through each water company in the dictionary and calculate the downstream impact of spills
# We suppress warnings to avoid cluttering the output.
warnings.filterwarnings("ignore")
for company, data in watercompany_info.items():
    print("#" * 50)
    print(f"Processing {company}...")

    # Get the WaterCompany object
    wc = data["WaterCompany"]

    # Define the local output directory and AWS folder name to put the outputs in
    local_output_dir = data["local_output_dir"]
    aws_folder = data["aws_folder_name"]
    geojson_file_name = wc.timestamp.strftime("%y%m%d_%H%M%S.geojson")
    geojson_file_name_excl_48hrs = geojson_file_name.replace(
        ".geojson", "_excl_48hrs.geojson"
    )
    info_geojson_file_name = wc.timestamp.strftime("%y%m%d_%H%M%S_info.geojson")
    info_geojson_file_name_excl_48hrs = info_geojson_file_name.replace(
        ".geojson", "_excl_48hrs.geojson"
    )

    ###### Calculate downstream impact geojsons ######

    # Calculate downstream impact of spills
    print("Calculating downstream impact of spills...")
    # ... including recent discharges (last 48hrs)
    geojson = wc.get_downstream_geojson(include_recent_discharges=True)
    # ... excluding recent discharges (i.e. only current spills)
    geojson_not_48hrs = wc.get_downstream_geojson(include_recent_discharges=False)

    print("Saving outputs locally...")
    # Save geojson to local directory
    # For legacy reasons we need to wrap the geojsons in a FeatureCollection...
    feature_coll = FeatureCollection(
        [Feature(geometry=geojson, type="MultiLineString")]
    )
    feature_coll = project_featurecollection_BNG_WGS84(feature_coll)

    feature_coll_not_48hrs = FeatureCollection(
        [Feature(geometry=geojson_not_48hrs, type="MultiLineString")]
    )
    feature_coll_not_48hrs = project_featurecollection_BNG_WGS84(feature_coll_not_48hrs)

    # Save the geojsons to the local directory
    with open(local_output_dir + geojson_file_name, "w") as f:
        json.dump(feature_coll, f)

    with open(local_output_dir + geojson_file_name_excl_48hrs, "w") as f:
        json.dump(feature_coll_not_48hrs, f)

    print("Uploading outputs to AWS bucket...")
    # Clear out the folder in the AWS bucket so it only contains the latest outputs
    empty_s3_folder(
        bucket_name=BUCKET_NAME, folder_name=aws_folder, profile_name=PROFILE_NAME
    )
    # Upload the geojsons to the AWS bucket
    upload_file_to_s3(
        file_path=local_output_dir + geojson_file_name,
        bucket_name=BUCKET_NAME,
        object_name=aws_folder + company + "_" + AWS_GEOJSON_FILENAME,
        profile_name=PROFILE_NAME,
    )
    upload_file_to_s3(
        file_path=local_output_dir + geojson_file_name_excl_48hrs,
        bucket_name=BUCKET_NAME,
        object_name=aws_folder + company + "_" + AWS_GEOJSON_FILENAME_EXCL_48HRS,
        profile_name=PROFILE_NAME,
    )

    ###### Now do the same for downstream *info* geojsons ######

    # Calculate further information about the downstream impact...
    print("Calculating further information about the downstream impact...")
    # ... including recent discharges (last 48hrs)
    info_geojson = wc.get_downstream_info_geojson(include_recent_discharges=True)
    # ... excluding recent discharges (i.e. only current spills)
    info_geojson_not_48hrs = wc.get_downstream_info_geojson(
        include_recent_discharges=False
    )
    info_feature_coll = project_featurecollection_BNG_WGS84(info_geojson)
    info_feature_coll_not_48hrs = project_featurecollection_BNG_WGS84(info_geojson_not_48hrs)

    with open(local_output_dir + info_geojson_file_name, "w") as f:
        json.dump(info_feature_coll, f)

    with open(local_output_dir + info_geojson_file_name_excl_48hrs, "w") as f:
        json.dump(info_feature_coll_not_48hrs, f)

    upload_file_to_s3(
            file_path=local_output_dir + info_geojson_file_name,
            bucket_name=BUCKET_NAME,
            object_name=aws_folder + company + "_" + AWS_INFO_GEOJSON_FILENAME,
            profile_name=PROFILE_NAME,
        )
    upload_file_to_s3(
        file_path=local_output_dir + info_geojson_file_name_excl_48hrs,
        bucket_name=BUCKET_NAME,
        object_name=aws_folder + company + "_" + AWS_INFO_GEOJSON_FILENAME_EXCL_48HRS,
        profile_name=PROFILE_NAME,
    )

    ###### To conclude, add a timestamp file to the folder ######

    # Add timestamp file to now folder
    write_timestamp(
        datetime_string=wc.timestamp.isoformat(timespec="seconds"),
        timestamp_filename=local_output_dir + TIMESTAMP_FILENAME,
    )
    upload_file_to_s3(
        file_path=local_output_dir + TIMESTAMP_FILENAME,
        bucket_name=BUCKET_NAME,
        object_name=aws_folder + company + "_" + TIMESTAMP_FILENAME,
        profile_name=PROFILE_NAME,
    )

    # Now delete the contents of local_output_dir
    print("Cleaning up local directory...")
    for file in os.listdir(local_output_dir):
        os.remove(local_output_dir + file)

# Add timestamp file to now folder ONLY AFTER SUCCESSFULLY UPDATING ALL OF THE WATER COMPANIES
write_timestamp(
    datetime_string=now.isoformat(timespec="seconds"),
    timestamp_filename=LOCAL_OUTPUT_DIR + "downstream_impact/global_timestamp.txt",
)
upload_file_to_s3(
    file_path=LOCAL_OUTPUT_DIR + "downstream_impact/global_timestamp.txt",
    bucket_name=BUCKET_NAME,
    object_name="downstream_impact/global_timestamp.txt",
    profile_name=PROFILE_NAME,
)

endtime = datetime.now()

print("#" * 50)
print("All done!")
# Print currenttime in readable format
print(f"Current time: {datetime.now().isoformat(timespec='seconds')}")
# Print how long the script took to run and print it in a readable format (minutes:seconds)
runtime = endtime - startime
print(f"Total runtime: {runtime.seconds//60} minutes {runtime.seconds%60} seconds")
print("#" * 50)
