#!/usr/bin/python
from datetime import datetime
import json
import os
import time

from geojson import Feature, FeatureCollection

from aux_funcs import (
    empty_s3_folder,
    upload_file_to_s3,
    write_timestamp,
    project_featurecollection_BNG_WGS84,
)

from poopy.companies import ThamesWater, WelshWater

# Name of the bucket to upload to
BUCKET_NAME = "thamessewage"

# Name of the AWS profile to use (set as an environment variable)
PROFILE_NAME = os.getenv("S3_PROFILE_NAME")
if PROFILE_NAME is None:
    raise ValueError(
        "AWS profile name is missing from the environment!\n Please set it and try again."
    )

# Local directory to save outputs to
LOCAL_OUTPUT_DIR = "output_dir/"
# Local directory to save geojsons to
LOCAL_GEOJSON_DIR = LOCAL_OUTPUT_DIR + "geojsons/"
# Local directory to save historical data to
LOCAL_HISTORICAL_DATA_DIR = LOCAL_OUTPUT_DIR + "discharges_to_date/"
# AWS directory to save current outputs to
AWS_NOW_DIR = "now/"
# AWS directory to save current info outputs to
AWS_INFO_NOW_DIR = "info_now/"
# AWS directory to save historical outputs to
AWS_HISTORICAL_DIR = "discharges_to_date/"
# AWS directory to save long-term outputs to
AWS_PAST_DIR = "past/"
# # AWS directory to store manual alerts to
# AWS_MANUAL_DIR = "manual_histories/"
# Name of the timestamp file to upload (locally + in AWS)
TIMESTAMP_FILENAME = "timestamp.txt"
# Name of the geojson files in the AWS bucket for current discharges
AWS_GEOJSON_FILENAME = "now.geojson"
# Nam of the geojson files in the AWS bucket for information on current discharges
AWS_INFO_GEOJSON_FILENAME = "info_now.geojson"
# Name of the json file in the AWS bucket for historical discharges
AWS_JSON_FILENAME = "up_to_now.json"
# Name of the json file in the AWS bucket for historical offline discharges
AWS_OFFLINE_JSON_FILENAME = "up_to_now_offline.json"
# Name of the log file
LOCAL_LOG = "history.log"
# Name of the AWS log folder
AWS_LOG_DIR = "history_log/"


def upload_downstream_impact_files_to_s3(
    geojson_file_path: str, timestamp: str
) -> None:
    """Uploads the downstream impact files to the ThamesSewage AWS bucket"""
    # Empty the 'now' folder
    empty_s3_folder(
        bucket_name=BUCKET_NAME, folder_name=AWS_NOW_DIR, profile_name=PROFILE_NAME
    )
    # Upload file to current 'now' output and also the long-term storage 'past' folder
    upload_file_to_s3(
        file_path=LOCAL_GEOJSON_DIR + geojson_file_path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_NOW_DIR + AWS_GEOJSON_FILENAME,
        profile_name=PROFILE_NAME,
    )
    upload_file_to_s3(
        file_path=LOCAL_GEOJSON_DIR + geojson_file_path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_PAST_DIR + geojson_file_path,
        profile_name=PROFILE_NAME,
    )
    # Add timestamp file to now folder
    write_timestamp(
        datetime_string=timestamp,
        timestamp_filename=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
    )
    upload_file_to_s3(
        file_path=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
        bucket_name=BUCKET_NAME,
        object_name=AWS_NOW_DIR + TIMESTAMP_FILENAME,
        profile_name=PROFILE_NAME,
    )


def upload_downstream_impact_info_files_to_s3(
    info_geojson_file_path: str, timestamp: str
) -> None:
    """Uploads the downstream impact info files to the ThamesSewage AWS bucket.
    These info files contain more specific information about the discharge impact."""
    # Empty the 'now' folder
    empty_s3_folder(
        bucket_name=BUCKET_NAME, folder_name=AWS_INFO_NOW_DIR, profile_name=PROFILE_NAME
    )
    # Upload file to current 'now' output and also the long-term storage 'past' folder
    upload_file_to_s3(
        file_path=LOCAL_GEOJSON_DIR + info_geojson_file_path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_INFO_NOW_DIR + AWS_INFO_GEOJSON_FILENAME,
        profile_name=PROFILE_NAME,
    )
    # Add timestamp file to info_now folder
    write_timestamp(
        datetime_string=timestamp,
        timestamp_filename=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
    )
    upload_file_to_s3(
        file_path=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
        bucket_name=BUCKET_NAME,
        object_name=AWS_INFO_NOW_DIR + TIMESTAMP_FILENAME,
        profile_name=PROFILE_NAME,
    )


def delete_historical_data_files_from_s3() -> None:
    """Deletes the historical data files from the ThamesSewage AWS bucket"""
    empty_s3_folder(
        bucket_name=BUCKET_NAME,
        folder_name=AWS_HISTORICAL_DIR,
        profile_name=PROFILE_NAME,
    )


def upload_historical_data_files_to_s3(
    json_file_path: str, offline_json_file_path: str, timestamp: str
) -> None:
    """Uploads the downstream impact files to the ThamesSewage AWS bucket"""
    upload_file_to_s3(
        file_path=LOCAL_HISTORICAL_DATA_DIR + json_file_path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_HISTORICAL_DIR + AWS_JSON_FILENAME,
        profile_name=PROFILE_NAME,
    )
    upload_file_to_s3(
        file_path=LOCAL_HISTORICAL_DATA_DIR + offline_json_file_path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_HISTORICAL_DIR + AWS_OFFLINE_JSON_FILENAME,
        profile_name=PROFILE_NAME,
    )
    # Add timestamp file to now folder
    write_timestamp(
        datetime_string=timestamp,
        timestamp_filename=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
    )
    upload_file_to_s3(
        file_path=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
        bucket_name=BUCKET_NAME,
        object_name=AWS_HISTORICAL_DIR + TIMESTAMP_FILENAME,
        profile_name=PROFILE_NAME,
    )


def main():
    print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
    startime = datetime.now()
    print("Starting @", datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    tw_clientID = os.getenv("TW_CLIENT_ID")
    tw_clientSecret = os.getenv("TW_CLIENT_SECRET")
    if tw_clientID is None or tw_clientSecret is None:
        raise ValueError(
            "Thames Water API keys are missing from the environment!\n Please set them and try again."
        )
    now = datetime.now()
    geojson_file_name = now.strftime("%y%m%d_%H%M%S.geojson")
    tw = ThamesWater(tw_clientID, tw_clientSecret)

    print("Calculating current downstream discharge extent...")
    geojson = tw.get_downstream_geojson(include_recent_discharges=True)
    # Save geojson to local directory

    # For legacy reasons we need to wrap the geojson in a FeatureCollection...
    feature_coll = FeatureCollection(
        [Feature(geometry=geojson, type="MultiLineString")]
    )
    feature_coll = project_featurecollection_BNG_WGS84(feature_coll)

    with open(LOCAL_GEOJSON_DIR + geojson_file_name, "w") as f:
        json.dump(feature_coll, f)
    print("Uploading outputs to AWS bucket")
    upload_downstream_impact_files_to_s3(
        geojson_file_path=geojson_file_name,
        timestamp=now.isoformat(timespec="seconds"),
    )

    print("Calculating current downstream discharge information...")
    info_geojson = tw.get_downstream_info_geojson(include_recent_discharges=True)
    info_geojson = project_featurecollection_BNG_WGS84(info_geojson)
    info_geojson_file_name = now.strftime("%y%m%d_%H%M%S_info.geojson")
    with open(LOCAL_GEOJSON_DIR + info_geojson_file_name, "w") as f:
        json.dump(info_geojson, f)
    print("Uploading outputs to AWS bucket")
    upload_downstream_impact_info_files_to_s3(
        info_geojson_file_path=info_geojson_file_name,
        timestamp=now.isoformat(timespec="seconds"),
    )

    print("Fetching historical event information...")
    # We do this first so that if the Thames Water API fails we aren't left with out of date data
    json_file_name = now.strftime("%y%m%d_%H%M%S.json")
    tw.set_all_histories()
    df = tw.history_to_discharge_df()
    # Fill in missing stop times (for ongoing discharges) with now for consistency with www.sewagemap.com legacy inputs
    df["StopDateTime"] = df["StopDateTime"].fillna(datetime.now())
    df.to_json(LOCAL_HISTORICAL_DATA_DIR + json_file_name)
    # Now do the same for Offline history
    offline_json_file_name = now.strftime("%y%m%d_%H%M%S_offline.json")
    off_df = tw.history_to_offline_df()
    # Fill in missing stop times (for ongoing events) with now for consistency with www.sewagemap.com legacy inputs
    off_df["StopDateTime"] = off_df["StopDateTime"].fillna(datetime.now())
    off_df.to_json(LOCAL_HISTORICAL_DATA_DIR + offline_json_file_name)

    print("Uploading outputs to AWS bucket")
    delete_historical_data_files_from_s3()
    upload_historical_data_files_to_s3(
        json_file_path=json_file_name,
        offline_json_file_path=offline_json_file_name,
        timestamp=now.isoformat(timespec="seconds"),
    )

    ### Commented out because experimented deemed too much work to bother with.... ###
    # print("Updating local experimental history tables for Welsh Water & Thames Water")

    # print("\tUpdating Thames Water...")
    # tw.update_alerts_table(verbose=True)
    # print("\tUpdating WelshWater...")
    # ww = WelshWater()
    # ww.update_alerts_table(verbose=True)

    # # Uploading them to AWS
    # # Empty the folder first
    # empty_s3_folder(
    #     bucket_name=BUCKET_NAME, folder_name=AWS_MANUAL_DIR, profile_name=PROFILE_NAME
    # )
    # # Upload all the relevant files
    # for file in [
    #     "WelshWater_alerts.csv",
    #     "ThamesWater_alerts.csv",
    #     "WelshWater_alerts_update_list.dat",
    #     "ThamesWater_alerts_update_list.dat",
    # ]:
    #     upload_file_to_s3(
    #         file_path=file,
    #         bucket_name=BUCKET_NAME,
    #         object_name=AWS_MANUAL_DIR + file,
    #         profile_name=PROFILE_NAME,
    #     )


    endtime = datetime.now()
    runtime = endtime - startime
    print("Finished @", datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    print(f"Total runtime: {runtime.seconds//60} minutes {runtime.seconds%60} seconds")
    print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")

    # Pause for 1 minute before uploading the cron-log
    time.sleep(60)
    print("Uploading cron-log...")

    # Empty the log folder
    empty_s3_folder(
        bucket_name=BUCKET_NAME, folder_name=AWS_LOG_DIR, profile_name=PROFILE_NAME
    )
    upload_file_to_s3(
        file_path=LOCAL_LOG,
        bucket_name=BUCKET_NAME,
        object_name=AWS_LOG_DIR + LOCAL_LOG,
        profile_name=PROFILE_NAME,
    )
if __name__ == "__main__":
    main()
