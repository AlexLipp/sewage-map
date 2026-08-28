import boto3
import json
from botocore.exceptions import ClientError
from osgeo import ogr, osr


def empty_s3_folder(bucket_name: str, folder_name: str, profile_name: str) -> None:
    """Empties a folder in an AWS bucket"""

    session = boto3.Session(profile_name=profile_name)
    s3 = session.client("s3")
    response = s3.list_objects_v2(Bucket=bucket_name, Prefix=folder_name)

    if "Contents" in response:
        # Construct the list of objects to delete
        objects_to_delete = [{"Key": obj["Key"]} for obj in response["Contents"]]

        # Perform the batch delete operation
        s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objects_to_delete})

        print(
            f"All objects in '\033[92m{folder_name}\033[0m' folder deleted successfully."
        )
    else:
        print(f"No objects found in '\033[92m{folder_name}\033[0m' folder.")


def download_file_from_s3(
    bucket_name: str, object_name: str, file_path: str, profile_name: str
) -> bool:
    """Downloads a file from an AWS bucket.

    Returns True if the object was downloaded, and False if it does not exist.
    Any other error is raised, so a transient S3 failure is never mistaken for a
    missing object (which callers may treat as "start from scratch")."""
    session = boto3.Session(profile_name=profile_name)
    s3 = session.client("s3")
    try:
        s3.download_file(bucket_name, object_name, file_path)
        print(
            f"File \033[92m{bucket_name}/{object_name}\033[0m downloaded to \033[92m{file_path}\033[0m successfully."
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            print(f"No object found at \033[93m{bucket_name}/{object_name}\033[0m.")
            return False
        raise


def upload_file_to_s3(
    file_path: str,
    bucket_name: str,
    object_name: str,
    profile_name: str,
    strict: bool = False,
):
    """Uploads a file to an AWS bucket.

    If strict is True, a failed upload raises instead of merely printing. Use
    strict for anything whose silent failure would leave artefacts inconsistent
    with each other (e.g. the long-lived history master)."""
    session = boto3.Session(profile_name=profile_name)
    s3 = session.client("s3")
    try:
        s3.upload_file(file_path, bucket_name, object_name)
        # Set cache-control headers to prevent caching
        s3.put_object_tagging(
            Bucket=bucket_name,
            Key=object_name,
            Tagging={"TagSet": [{"Key": "Cache-Control", "Value": "no-cache"}]},
        )
        # Write print(f"File {file_path} uploaded to {bucket_name}/{object_name} successfully."), but make the file paths and destinations in red using ANSI escape codes
        print(
            f"File \033[92m{file_path}\033[0m uploaded to \033[92m{bucket_name}/{object_name}\033[0m successfully."
        )

    except Exception as e:
        print(f"Error uploading file: {str(e)}")
        if strict:
            raise


def write_timestamp(datetime_string: str, timestamp_filename: str):
    """Writes a file that contains "datetime_string" to file "timestamp_filename"""
    try:
        with open(timestamp_filename, "w") as file:
            file.write(datetime_string)
        print(
            f"Successfully wrote timestamp \033[92m{datetime_string}\033[0m to \033[92m{timestamp_filename}\033[0m."
        )
    except Exception as e:
        print(f"An error occurred: {e}")


def project_geojson_BNG_WGS84(geojson: dict) -> dict:
    """Projects a geojson from BNG to WGS84"""
    source_srs = osr.SpatialReference()
    source_srs.ImportFromEPSG(27700)  # British National Grid
    target_srs = osr.SpatialReference()
    target_srs.ImportFromEPSG(4326)  # WGS84
    transform = osr.CoordinateTransformation(source_srs, target_srs)
    geom = ogr.CreateGeometryFromJson(json.dumps(geojson))
    geom.Transform(transform)
    return json.loads(geom.ExportToJson())


def project_featurecollection_BNG_WGS84(feature_collection: dict) -> dict:
    """Projects a FeatureCollection from BNG to WGS84"""
    # Define source and target spatial references
    source_srs = osr.SpatialReference()
    source_srs.ImportFromEPSG(27700)  # British National Grid
    target_srs = osr.SpatialReference()
    target_srs.ImportFromEPSG(4326)  # WGS84

    # Create a coordinate transformation
    transform = osr.CoordinateTransformation(source_srs, target_srs)

    # Iterate over each feature in the FeatureCollection
    for feature in feature_collection["features"]:
        # Convert the feature's geometry to a JSON string
        geom_json = json.dumps(feature["geometry"])
        # Create an OGR geometry from the JSON string
        geom = ogr.CreateGeometryFromJson(geom_json)
        # Apply the coordinate transformation to the geometry
        geom.Transform(transform)
        # Update the feature's geometry with the transformed geometry
        feature["geometry"] = json.loads(geom.ExportToJson())

    # Return the updated FeatureCollection
    return feature_collection
