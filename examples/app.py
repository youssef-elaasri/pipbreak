"""A small service written with an AI assistant. It looks fine. Several calls are wrong."""
import collections
import itertools
import os.path
import random
import shutil
from pathlib import Path

from pydantic import BaseModel, BaseSettings, field_validator, validator


class Settings(BaseSettings):
    data_dir: str = "data"


class User(BaseModel):
    name: str
    email: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return value.lower()

    @validator("name")
    def strip_name(cls, value):
        return value.strip()


def backup(src, dst):
    if os.path.exist(dst):
        return
    shutil.copy(src, dst, overwrite=True)


def sample(items):
    return random.choices(items, k=3, unique=True)


def chunks(items):
    return list(itertools.batched(items, 10))


def is_mapping(value):
    return isinstance(value, collections.Mapping)


def load_config(name):
    path = Path(name)
    if not path.exist():
        return ""
    return path.read_text(encodings="utf-8")


def export(user: User):
    return user.dict()


def parse(data):
    return User.parse_obj(data)


class Job:
    def __init__(self, name):
        self.name = name
        self.retries = 0


def should_retry(name):
    job = Job(name)
    return job.retry_count < 3
