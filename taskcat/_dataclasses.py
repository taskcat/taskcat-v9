import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, NewType, Optional, Union

import boto3
from dataclasses_jsonschema import FieldEncoder, JsonSchemaMixin

from taskcat._cfn.template import Template
from taskcat._client_factory import Boto3Cache
from taskcat._common_utils import merge_nested_dict
from taskcat.exceptions import TaskCatException

LOG = logging.getLogger(__name__)


class StrictSchema(JsonSchemaMixin):
    allow_unencodable_object_keys = False


# types

ParameterKey = NewType("ParameterKey", str)
ParameterValue = Union[str, int, bool, List[Union[int, str]]]
TagKey = NewType("TagKey", str)
TagValue = NewType("TagValue", str)
Region = NewType("Region", str)
AlNumDash = NewType("AlNumDash", str)
ProjectName = NewType("ProjectName", AlNumDash)
S3BucketName = NewType("S3BucketName", AlNumDash)
TestName = NewType("TestName", AlNumDash)
AzId = NewType("AzId", str)
Templates = NewType("Templates", Dict[TestName, Template])

# regex validation


class ParameterKeyField(FieldEncoder):
    @property
    def json_schema(self):
        return {"type": "string", "pattern": r"[a-zA-Z0-9]*^$"}


StrictSchema.register_field_encoders({ParameterKey: ParameterKeyField()})


class RegionField(FieldEncoder):
    @property
    def json_schema(self):
        return {
            "type": "string",
            "pattern": r"^(ap|eu|us|sa|ca|cn|af|me|us-gov)-(central|south|north|east|"
            r"west|southeast|southwest|northeast|northwest)-[0-9]$",
        }


StrictSchema.register_field_encoders({Region: RegionField()})


class AlNumDashField(FieldEncoder):
    @property
    def json_schema(self):
        return {"type": "string", "pattern": r"^[a-z/d-]*$"}


StrictSchema.register_field_encoders({AlNumDash: AlNumDashField()})


class AzIdField(FieldEncoder):
    @property
    def json_schema(self):
        return {
            "type": "string",
            "pattern": r"^(ap|eu|us|sa|ca|cn|af|me)(n|s|e|w|c|ne|se|nw|sw)[0-9]-az[0-9]"
            r"$",
        }


StrictSchema.register_field_encoders({AzId: AzIdField()})


#


# dataclasses
@dataclass
class RegionObj:
    name: str
    account_id: str
    partition: str
    profile: str
    taskcat_id: uuid.UUID
    _boto3_cache: Boto3Cache

    def client(self, service: str):
        return self._boto3_cache.client(service, region=self.name, profile=self.profile)

    @property
    def session(self):
        return self._boto3_cache.session(region=self.name, profile=self.profile)


@dataclass
class S3BucketObj:
    name: str
    region: str
    account_id: str
    partition: str
    s3_client: boto3.client
    sigv4: bool
    auto_generated: bool
    object_acl: str
    taskcat_id: uuid.UUID

    @property
    def sigv4_policy(self):
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "Test",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": f"arn:aws:s3:::{self.name}/*",
                    "Condition": {"StringEquals": {"s3:signatureversion": "AWS"}},
                }
            ],
        }
        return json.dumps(policy)

    def create(self):
        if self._bucket_matches_existing():
            return
        kwargs = {"Bucket": self.name}
        if self.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}

        self.s3_client.create_bucket(**kwargs)
        error = None
        try:
            self.s3_client.get_waiter("bucket_exists").wait(Bucket=self.name)

            self.s3_client.put_bucket_tagging(
                Bucket=self.name,
                Tagging={
                    "TagSet": [{"Key": "taskcat-id", "Value": self.taskcat_id.hex}]
                },
            )
            if self.sigv4:
                self.s3_client.put_bucket_policy(
                    Bucket=self.name, Policy=self.sigv4_policy
                )
        except Exception as e:  # pylint: disable=broad-except
            error = e
            try:
                self.s3_client.delete_bucket(Bucket=self.name)
            except Exception as inner_e:  # pylint: disable=broad-except
                LOG.warning(f"failed to remove bucket {self.name}: {inner_e}")
        if error:
            raise error

    def empty(self):
        if not self.auto_generated:
            LOG.error(f"Will not empty bucket created outside of taskcat {self.name}")
            return
        objects_to_delete = []
        pages = self.s3_client.get_paginator("list_objects_v2").paginate(
            Bucket=self.name
        )
        for page in pages:
            objects = []
            for obj in page.get("Contents", []):
                del_obj = {"Key": obj["Key"]}
                if obj.get("VersionId"):
                    del_obj["VersionId"] = obj["VersionId"]
                objects.append(del_obj)
            objects_to_delete += objects
        batched_objects = [
            objects_to_delete[i : i + 1000]
            for i in range(0, len(objects_to_delete), 1000)
        ]
        for objects in batched_objects:
            if objects:
                self.s3_client.delete_objects(
                    Bucket=self.name, Delete={"Objects": objects}
                )

    def delete(self, delete_objects=False):
        if not self.auto_generated:
            LOG.error(f"Will not delete bucket created outside of taskcat {self.name}")
            return
        if delete_objects:
            try:
                self.empty()
            except self.s3_client.exceptions.NoSuchBucket:
                LOG.info(f"Cannot delete bucket {self.name} as it does not exist")
                return
        try:
            self.s3_client.delete_bucket(Bucket=self.name)
        except self.s3_client.exceptions.NoSuchBucket:
            LOG.info(f"Cannot delete bucket {self.name} as it does not exist")

    def _bucket_matches_existing(self):
        try:
            location = self.s3_client.get_bucket_location(Bucket=self.name)[
                "LocationConstraint"
            ]
            location = location if location else "us-east-1"
        except self.s3_client.exceptions.NoSuchBucket:
            location = None
        if location != self.region and location is not None:
            raise TaskCatException(
                f"bucket {self.name} already exists, but is not in "
                f"the expected region {self.region}, expected {location}"
            )
        if location:
            tags = self.s3_client.get_bucket_tagging(Bucket=self.name)["TagSet"]
            tags = {t["Key"]: t["Value"] for t in tags}
            uid = tags.get("taskcat-id")
            uid = uuid.UUID(uid) if uid else uid
            if uid != self.taskcat_id:
                raise TaskCatException(
                    f"bucket {self.name} already exists, but does not have a matching"
                    f" uuid"
                )
            return True
        return False


@dataclass
class TestRegion(RegionObj):
    s3_bucket: S3BucketObj
    parameters: Dict[ParameterKey, ParameterValue]

    @classmethod
    def from_region_obj(cls, region: RegionObj, s3_bucket, parameters):
        return cls(s3_bucket=s3_bucket, parameters=parameters, **region.__dict__)


@dataclass
class TestObj:
    template_path: Path
    template: Template
    project_root: Path
    name: TestName
    regions: List[TestRegion]


@dataclass
class GeneralConfig(StrictSchema):
    parameters: Optional[Dict[ParameterKey, ParameterValue]] = field(default=None)
    tags: Optional[Dict[TagKey, TagValue]] = field(default=None)
    auth: Optional[Dict[Region, str]] = field(default=None)
    s3_bucket: Optional[str] = field(default=None)


@dataclass
class TestConfig(StrictSchema):
    template: Optional[str] = field(default=None)
    parameters: Optional[Dict[ParameterKey, ParameterValue]] = field(default=None)
    regions: Optional[List[Region]] = field(default=None)
    tags: Optional[Dict[TagKey, TagValue]] = field(default=None)
    auth: Optional[Dict[Region, str]] = field(default=None)
    s3_bucket: Optional[S3BucketName] = field(default=None)
    az_blacklist: Optional[List[AzId]] = field(default=None)


@dataclass
class ProjectConfig(StrictSchema):
    name: Optional[ProjectName] = field(default=None)
    auth: Optional[Dict[Region, str]] = field(default=None)
    owner: Optional[str] = field(default=None)
    regions: Optional[List[Region]] = field(default=None)
    az_blacklist: Optional[List[AzId]] = field(default=None)
    package_lambda: Optional[bool] = field(default=None)
    lambda_zip_path: Optional[str] = field(default=None)
    lambda_source_path: Optional[str] = field(default=None)
    s3_bucket: Optional[S3BucketName] = field(default=None)
    parameters: Optional[Dict[ParameterKey, ParameterValue]] = field(default=None)
    build_submodules: Optional[bool] = field(default=None)
    template: Optional[str] = field(default=None)
    tags: Optional[Dict[TagKey, TagValue]] = field(default=None)
    s3_enable_sig_v2: Optional[bool] = field(default=None)
    s3_object_acl: Optional[str] = field(default=None)


PROPAGATE_KEYS = ["tags", "parameters", "auth"]
PROPOGATE_ITEMS = ["regions", "s3_bucket", "template", "az_blacklist"]


# pylint raises false positive due to json-dataclass
# pylint: disable=no-member
@dataclass
class BaseConfig(StrictSchema):
    general: GeneralConfig = field(default_factory=GeneralConfig)
    project: ProjectConfig = field(default_factory=ProjectConfig)
    tests: Dict[TestName, TestConfig] = field(default_factory=dict)

    # pylint doesn't like instance variables being added in post_init
    # pylint: disable=attribute-defined-outside-init
    def __post_init__(self):
        self._source: Dict[str, Any] = {}
        self._propogate()
        self.set_source("UNKNOWN")
        self._propogate_source()

    @staticmethod
    def _merge(source, dest):
        for section_key, section_value in source.items():
            if section_key in PROPAGATE_KEYS + PROPOGATE_ITEMS:
                if section_key not in dest:
                    dest[section_key] = section_value
                    continue
                if section_key in PROPAGATE_KEYS:
                    for key, value in section_value.items():
                        dest[section_key][key] = value
        return dest

    def _propogate(self):
        project_dict = self._merge(self.general.to_dict(), self.project.to_dict())
        self.project = ProjectConfig.from_dict(project_dict)
        for test_key, test in self.tests.items():
            test_dict = self._merge(self.project.to_dict(), test.to_dict())
            self.tests[test_key] = TestConfig.from_dict(test_dict)

    def _propogate_source(self):
        self._source["project"] = self._merge(
            self._source["general"], self._source["project"]
        )
        for test_key in self._source["tests"]:
            test = self._merge(self._source["project"], self._source["tests"][test_key])
            self._source["tests"][test_key] = test

    def set_source(
        self, source_name: str, dest: Optional[Any] = None
    ) -> Optional[Union[str, dict]]:
        base_case = False
        if dest is None:
            base_case = True
            self._source = self.to_dict()
            dest = self._source
        if not isinstance(dest, dict):
            return source_name
        if isinstance(dest, dict):
            for item in dest:
                dest[item] = self.set_source(source_name, dest[item])
        if not base_case:
            return dest
        return None

    @classmethod
    def merge(
        cls, base_config: "BaseConfig", merge_config: "BaseConfig"
    ) -> "BaseConfig":

        merged = base_config.to_dict()
        merge_nested_dict(merged, merge_config.to_dict())

        merged_source = base_config._source.copy()
        merge_nested_dict(merged_source, merge_config._source)

        config = cls.from_dict(merged)

        config._source = merged_source
        config._propogate_source()  # pylint: disable=protected-access
        return config