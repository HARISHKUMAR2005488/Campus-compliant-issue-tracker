"""
aws.py — Campus Complaint & Issue Tracker
AWS Backend using Flask + boto3

Services used:
  - AWS DynamoDB : stores users and issues
  - AWS S3       : stores uploaded issue images

Local dev setup:
  1. pip install -r requirements.txt
  2. Copy .env.example -> .env and fill in your values
  3. Create DynamoDB table : campus_tracker  (partition key = id, type String)
  4. Create S3 bucket      : <your-bucket-name>  (block public access ON; presigned URLs used)
  5. Run: python aws.py

EC2 production setup:
  1. Attach an IAM Role to the EC2 instance with DynamoDB + S3 permissions
     (leave AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY blank in .env)
  2. Run via gunicorn: gunicorn -c gunicorn.conf.py aws:app
  3. Nginx reverse-proxies port 80 -> gunicorn on port 5000
  4. App serves index.html from GET /
"""

import os
import uuid
import base64
import mimetypes
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

AWS_REGION        = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID") or None   # None = use IAM Role on EC2
AWS_SECRET_KEY    = os.getenv("AWS_SECRET_ACCESS_KEY") or None
DYNAMODB_TABLE    = os.getenv("DYNAMODB_TABLE", "campus_tracker")
S3_BUCKET         = os.getenv("S3_BUCKET", "campus-tracker-images")
PRESIGNED_EXPIRY  = int(os.getenv("PRESIGNED_EXPIRY", 3600))   # seconds
ALLOWED_ORIGINS   = os.getenv("ALLOWED_ORIGINS", "*")          # restrict in production

# Base directory (same folder as this file) — used to serve index.html
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# AWS clients
# On EC2 with an IAM Role attached, omit access key / secret key so boto3
# automatically picks up the instance-profile credentials.
# ---------------------------------------------------------------------------
_session_kwargs = {"region_name": AWS_REGION}
if AWS_ACCESS_KEY_ID and AWS_SECRET_KEY:
    _session_kwargs["aws_access_key_id"]     = AWS_ACCESS_KEY_ID
    _session_kwargs["aws_secret_access_key"] = AWS_SECRET_KEY

_session  = boto3.Session(**_session_kwargs)
dynamodb  = _session.resource("dynamodb")
s3_client = _session.client("s3")
table     = dynamodb.Table(DYNAMODB_TABLE)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=BASE_DIR)
CORS(app, origins=ALLOWED_ORIGINS)   # restrict origins in production via ALLOWED_ORIGINS env


# ===========================================================================
# Helper utilities
# ===========================================================================

def now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def upload_image_to_s3(base64_data: str, prefix: str = "issues") -> str | None:
    """
    Upload a base64-encoded image to S3 and return the object key.
    Returns None on failure.

    base64_data may be a raw base64 string or a data-URI
    (e.g. 'data:image/png;base64,...').
    """
    if not base64_data:
        return None

    # Strip data-URI header if present
    if "," in base64_data:
        header, base64_data = base64_data.split(",", 1)
        # Detect MIME type from header (e.g. 'data:image/jpeg;base64')
        mime = header.split(":")[1].split(";")[0] if ":" in header else "image/jpeg"
    else:
        mime = "image/jpeg"

    ext = mimetypes.guess_extension(mime) or ".jpg"
    ext = ext.lstrip(".")
    key = f"{prefix}/{uuid.uuid4()}.{ext}"

    try:
        image_bytes = base64.b64decode(base64_data)
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=image_bytes,
            ContentType=mime,
        )
        return key
    except (ClientError, Exception) as exc:
        app.logger.error("S3 upload failed: %s", exc)
        return None


def get_presigned_url(key: str) -> str | None:
    """Generate a presigned GET URL for an S3 object."""
    if not key:
        return None
    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=PRESIGNED_EXPIRY,
        )
        return url
    except ClientError as exc:
        app.logger.error("Presigned URL generation failed: %s", exc)
        return None


def enrich_item(item: dict) -> dict:
    """Add a presigned image URL to an item if it has an imageKey."""
    if item.get("imageKey"):
        item["imageUrl"] = get_presigned_url(item["imageKey"])
    return item


# ===========================================================================
# Serve frontend
# ===========================================================================

@app.route("/", methods=["GET"])
def serve_index():
    """Serve the main HTML application."""
    return send_from_directory(BASE_DIR, "index.html")


# ===========================================================================
# Health check
# ===========================================================================

@app.route("/api/health", methods=["GET"])
def health():
    """Simple liveness probe."""
    return jsonify({"status": "ok", "timestamp": now_iso()})


# ===========================================================================
# Data SDK compatibility layer
# (mirrors the dataSdk.init / create / update / delete contract used in
#  index.html so you can swap the SDK calls for fetch() calls)
# ===========================================================================

@app.route("/api/data", methods=["GET"])
def get_all_data():
    """
    Return all items from DynamoDB.
    Equivalent to the initial data load in dataSdk.init().
    """
    try:
        response = table.scan()
        items = response.get("Items", [])

        # Handle DynamoDB pagination
        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))

        # Enrich items with presigned image URLs
        items = [enrich_item(i) for i in items]
        return jsonify({"isOk": True, "data": items})
    except ClientError as exc:
        app.logger.error("DynamoDB scan failed: %s", exc)
        return jsonify({"isOk": False, "error": str(exc)}), 500


@app.route("/api/data", methods=["POST"])
def create_item():
    """
    Create a new item in DynamoDB.
    Equivalent to dataSdk.create(data).

    Accepts JSON body. If the body contains 'imageData' (base64),
    the image is uploaded to S3 and the key is stored as 'imageKey'.
    """
    body = request.get_json(force=True, silent=True) or {}

    # Generate a unique ID
    item_id = str(uuid.uuid4())
    body["id"] = item_id
    body["__backendId"] = item_id   # keep parity with SDK field name

    # Handle image upload
    image_data = body.pop("imageData", "")
    if image_data:
        key = upload_image_to_s3(image_data)
        if key:
            body["imageKey"] = key
        else:
            app.logger.warning("Image upload failed; continuing without image.")

    body.setdefault("createdAt", now_iso())
    body.setdefault("updatedAt", now_iso())

    try:
        table.put_item(Item=body)
        return jsonify({"isOk": True, "id": item_id, "item": enrich_item(body)}), 201
    except ClientError as exc:
        app.logger.error("DynamoDB put_item failed: %s", exc)
        return jsonify({"isOk": False, "error": str(exc)}), 500


@app.route("/api/data/<item_id>", methods=["GET"])
def get_item(item_id: str):
    """Fetch a single item by ID."""
    try:
        response = table.get_item(Key={"id": item_id})
        item = response.get("Item")
        if not item:
            return jsonify({"isOk": False, "error": "Item not found"}), 404
        return jsonify({"isOk": True, "item": enrich_item(item)})
    except ClientError as exc:
        return jsonify({"isOk": False, "error": str(exc)}), 500


@app.route("/api/data/<item_id>", methods=["PUT"])
def update_item(item_id: str):
    """
    Update an existing item.
    Equivalent to dataSdk.update(id, data).
    """
    body = request.get_json(force=True, silent=True) or {}
    body["updatedAt"] = now_iso()

    # Handle image replacement
    image_data = body.pop("imageData", "")
    if image_data:
        key = upload_image_to_s3(image_data)
        if key:
            body["imageKey"] = key

    # Build DynamoDB update expression dynamically
    update_expr_parts = []
    expr_attr_names = {}
    expr_attr_values = {}

    for field, value in body.items():
        if field == "id":
            continue
        safe_key = f"#f_{field}"
        val_key = f":v_{field}"
        update_expr_parts.append(f"{safe_key} = {val_key}")
        expr_attr_names[safe_key] = field
        expr_attr_values[val_key] = value

    if not update_expr_parts:
        return jsonify({"isOk": False, "error": "No fields to update"}), 400

    update_expression = "SET " + ", ".join(update_expr_parts)

    try:
        response = table.update_item(
            Key={"id": item_id},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expr_attr_names,
            ExpressionAttributeValues=expr_attr_values,
            ReturnValues="ALL_NEW",
        )
        updated = response.get("Attributes", {})
        return jsonify({"isOk": True, "item": enrich_item(updated)})
    except ClientError as exc:
        app.logger.error("DynamoDB update_item failed: %s", exc)
        return jsonify({"isOk": False, "error": str(exc)}), 500


@app.route("/api/data/<item_id>", methods=["DELETE"])
def delete_item(item_id: str):
    """
    Delete an item by ID.
    Also removes the associated S3 image if one exists.
    """
    try:
        # Fetch item first to get imageKey
        response = table.get_item(Key={"id": item_id})
        item = response.get("Item", {})
        image_key = item.get("imageKey")

        table.delete_item(Key={"id": item_id})

        # Clean up S3 image
        if image_key:
            try:
                s3_client.delete_object(Bucket=S3_BUCKET, Key=image_key)
            except ClientError as exc:
                app.logger.warning("S3 delete failed (non-fatal): %s", exc)

        return jsonify({"isOk": True, "id": item_id})
    except ClientError as exc:
        app.logger.error("DynamoDB delete_item failed: %s", exc)
        return jsonify({"isOk": False, "error": str(exc)}), 500


# ===========================================================================
# Convenience domain-specific endpoints
# ===========================================================================

@app.route("/api/users", methods=["GET"])
def list_users():
    """List all registered users (type == 'user')."""
    try:
        response = table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr("type").eq("user")
        )
        users = response.get("Items", [])
        # Never return passwords to the client
        for u in users:
            u.pop("password", None)
        return jsonify({"isOk": True, "users": users})
    except ClientError as exc:
        return jsonify({"isOk": False, "error": str(exc)}), 500


@app.route("/api/issues", methods=["GET"])
def list_issues():
    """
    List issues with optional query-string filters:
      ?status=Pending
      ?priority=High
      ?category=Electrical
      ?reportedBy=student@campus.edu
    """
    status   = request.args.get("status")
    priority = request.args.get("priority")
    category = request.args.get("category")
    reporter = request.args.get("reportedBy")

    from boto3.dynamodb.conditions import Attr

    filter_expr = Attr("type").eq("issue")
    if status:
        filter_expr = filter_expr & Attr("status").eq(status)
    if priority:
        filter_expr = filter_expr & Attr("priority").eq(priority)
    if category:
        filter_expr = filter_expr & Attr("issueCategory").eq(category)
    if reporter:
        filter_expr = filter_expr & Attr("reportedBy").eq(reporter)

    try:
        response = table.scan(FilterExpression=filter_expr)
        issues = response.get("Items", [])
        issues = [enrich_item(i) for i in issues]
        # Sort newest first
        issues.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return jsonify({"isOk": True, "issues": issues})
    except ClientError as exc:
        return jsonify({"isOk": False, "error": str(exc)}), 500


@app.route("/api/issues/<issue_id>/status", methods=["PATCH"])
def update_issue_status(issue_id: str):
    """
    Quickly update an issue's status and optional admin comment.
    Body: { "status": "Resolved", "adminComment": "Fixed." }
    """
    body = request.get_json(force=True, silent=True) or {}
    new_status = body.get("status")
    admin_comment = body.get("adminComment", "")

    if not new_status:
        return jsonify({"isOk": False, "error": "'status' is required"}), 400

    update_data: dict = {
        "status": new_status,
        "updatedAt": now_iso(),
    }
    if admin_comment:
        update_data["adminComment"] = admin_comment
    if new_status == "Resolved":
        update_data["resolvedAt"] = now_iso()

    update_expr_parts = []
    expr_attr_names = {}
    expr_attr_values = {}
    for field, value in update_data.items():
        safe_key = f"#f_{field}"
        val_key = f":v_{field}"
        update_expr_parts.append(f"{safe_key} = {val_key}")
        expr_attr_names[safe_key] = field
        expr_attr_values[val_key] = value

    try:
        response = table.update_item(
            Key={"id": issue_id},
            UpdateExpression="SET " + ", ".join(update_expr_parts),
            ExpressionAttributeNames=expr_attr_names,
            ExpressionAttributeValues=expr_attr_values,
            ReturnValues="ALL_NEW",
        )
        return jsonify({"isOk": True, "item": enrich_item(response.get("Attributes", {}))})
    except ClientError as exc:
        return jsonify({"isOk": False, "error": str(exc)}), 500


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    using_iam = not (AWS_ACCESS_KEY_ID and AWS_SECRET_KEY)
    print(f"Starting Campus Tracker AWS backend on http://0.0.0.0:{port}")
    print(f"  DynamoDB table : {DYNAMODB_TABLE}")
    print(f"  S3 bucket      : {S3_BUCKET}")
    print(f"  AWS region     : {AWS_REGION}")
    print(f"  Auth mode      : {'IAM Role (EC2 instance profile)' if using_iam else 'Access Key'}")
    print(f"  Frontend served at http://0.0.0.0:{port}/")
    app.run(host="0.0.0.0", port=port, debug=debug)


# ===========================================================================
# .env template — copy .env.example to .env and fill in your values
# On EC2 with IAM Role: leave AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY blank
# ===========================================================================
# AWS_ACCESS_KEY_ID=          # leave blank on EC2 with IAM Role
# AWS_SECRET_ACCESS_KEY=      # leave blank on EC2 with IAM Role
# AWS_REGION=us-east-1
# DYNAMODB_TABLE=campus_tracker
# S3_BUCKET=campus-tracker-images
# PRESIGNED_EXPIRY=3600
# PORT=5000
# FLASK_DEBUG=false
# ALLOWED_ORIGINS=*           # restrict to your domain in production
