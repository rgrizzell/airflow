#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import logging
import socket
import string
import textwrap
from functools import wraps
from typing import TYPE_CHECKING, Callable, Optional, TypeVar, cast

from airflow.configuration import conf
from airflow.exceptions import AirflowConfigException, InvalidStatsNameException
from airflow.typing_compat import Protocol

log = logging.getLogger(__name__)


class StatsLogger(Protocol):
    """This class is only used for TypeChecking (for IDEs, mypy, pylint, etc)"""
    @classmethod
    def incr(cls, stat: str, count: int = 1, rate: int = 1) -> None:
        """Increment stat"""

    @classmethod
    def decr(cls, stat: str, count: int = 1, rate: int = 1) -> None:
        """Decrement stat"""

    @classmethod
    def gauge(cls, stat: str, value: float, rate: int = 1, delta: bool = False) -> None:
        """Gauge stat"""

    @classmethod
    def timing(cls, stat: str, dt) -> None:
        """Stats timing"""


class DummyStatsLogger:
    """If no StatsLogger is configured, DummyStatsLogger is used as a fallback"""
    @classmethod
    def incr(cls, stat, count=1, rate=1):
        """Increment stat"""

    @classmethod
    def decr(cls, stat, count=1, rate=1):
        """Decrement stat"""

    @classmethod
    def gauge(cls, stat, value, rate=1, delta=False):
        """Gauge stat"""

    @classmethod
    def timing(cls, stat, dt):
        """Stats timing"""


# Only characters in the character set are considered valid
# for the stat_name if stat_name_default_handler is used.
ALLOWED_CHARACTERS = set(string.ascii_letters + string.digits + '_.-')


def stat_name_default_handler(stat_name, max_length=250) -> str:
    """A function that validate the statsd stat name, apply changes to the stat name
    if necessary and return the transformed stat name.
    """
    if not isinstance(stat_name, str):
        raise InvalidStatsNameException('The stat_name has to be a string')
    if len(stat_name) > max_length:
        raise InvalidStatsNameException(textwrap.dedent("""\
            The stat_name ({stat_name}) has to be less than {max_length} characters.
        """.format(stat_name=stat_name, max_length=max_length)))
    if not all((c in ALLOWED_CHARACTERS) for c in stat_name):
        raise InvalidStatsNameException(textwrap.dedent("""\
            The stat name ({stat_name}) has to be composed with characters in
            {allowed_characters}.
            """.format(stat_name=stat_name,
                       allowed_characters=ALLOWED_CHARACTERS)))
    return stat_name


def get_current_handler_stat_name_func() -> Callable[[str], str]:
    """Get Stat Name Handler from airflow.cfg"""
    return conf.getimport('scheduler', 'stat_name_handler') or stat_name_default_handler


T = TypeVar("T", bound=Callable)  # pylint: disable=invalid-name


def validate_stat(fn: T) -> T:
    """Check if stat name contains invalid characters.
    Log and not emit stats if name is invalid
    """
    @wraps(fn)
    def wrapper(_self, stat, *args, **kwargs):
        try:
            handler_stat_name_func = get_current_handler_stat_name_func()
            stat_name = handler_stat_name_func(stat)
            return fn(_self, stat_name, *args, **kwargs)
        except InvalidStatsNameException:
            log.error('Invalid stat name: %s.', stat, exc_info=True)
            return None

    return cast(T, wrapper)


class AllowListValidator:
    """Class to filter unwanted stats"""

    def __init__(self, allow_list=None):
        if allow_list:
            self.allow_list = tuple([item.strip().lower() for item in allow_list.split(',')])
        else:
            self.allow_list = None

    def test(self, stat):
        """Test if stat is in the Allow List"""
        if self.allow_list is not None:
            return stat.strip().lower().startswith(self.allow_list)
        else:
            return True  # default is all metrics allowed


class SafeStatsdLogger:
    """Statsd Logger"""

    def __init__(self, statsd_client, allow_list_validator=AllowListValidator()):
        self.statsd = statsd_client
        self.allow_list_validator = allow_list_validator

    @validate_stat
    def incr(self, stat, count=1, rate=1):
        """Increment stat"""
        if self.allow_list_validator.test(stat):
            return self.statsd.incr(stat, count, rate)
        return None

    @validate_stat
    def decr(self, stat, count=1, rate=1):
        """Decrement stat"""
        if self.allow_list_validator.test(stat):
            return self.statsd.decr(stat, count, rate)
        return None

    @validate_stat
    def gauge(self, stat, value, rate=1, delta=False):
        """Gauge stat"""
        if self.allow_list_validator.test(stat):
            return self.statsd.gauge(stat, value, rate, delta)
        return None

    @validate_stat
    def timing(self, stat, dt):
        """Stats timing"""
        if self.allow_list_validator.test(stat):
            return self.statsd.timing(stat, dt)
        return None


class SafeDogStatsdLogger:
    """DogStatsd Logger"""

    def __init__(self, dogstatsd_client, allow_list_validator=AllowListValidator()):
        self.dogstatsd = dogstatsd_client
        self.allow_list_validator = allow_list_validator

    @validate_stat
    def incr(self, stat, count=1, rate=1, tags=None):
        """Increment stat"""
        if self.allow_list_validator.test(stat):
            tags = tags or []
            return self.dogstatsd.increment(metric=stat, value=count, tags=tags, sample_rate=rate)
        return None

    @validate_stat
    def decr(self, stat, count=1, rate=1, tags=None):
        """Decrement stat"""
        if self.allow_list_validator.test(stat):
            tags = tags or []
            return self.dogstatsd.decrement(metric=stat, value=count, tags=tags, sample_rate=rate)
        return None

    @validate_stat
    def gauge(self, stat, value, rate=1, delta=False, tags=None):  # pylint: disable=unused-argument
        """Gauge stat"""
        if self.allow_list_validator.test(stat):
            tags = tags or []
            return self.dogstatsd.gauge(metric=stat, value=value, tags=tags, sample_rate=rate)
        return None

    @validate_stat
    def timing(self, stat, dt, tags=None):
        """Stats timing"""
        if self.allow_list_validator.test(stat):
            tags = tags or []
            return self.dogstatsd.timing(metric=stat, value=dt, tags=tags)
        return None


class _Stats(type):
    instance: Optional[StatsLogger] = None

    def __getattr__(cls, name):
        return getattr(cls.instance, name)

    def __init__(cls, *args, **kwargs):
        super().__init__(cls)
        if cls.__class__.instance is None:
            try:
                is_datadog_enabled_defined = conf.has_option('scheduler', 'statsd_datadog_enabled')
                if is_datadog_enabled_defined and conf.getboolean('scheduler', 'statsd_datadog_enabled'):
                    cls.__class__.instance = cls.get_dogstatsd_logger()
                elif conf.getboolean('scheduler', 'statsd_on'):
                    cls.__class__.instance = cls.get_statsd_logger()
                else:
                    cls.__class__.instance = DummyStatsLogger()
            except (socket.gaierror, ImportError) as e:
                log.error("Could not configure StatsClient: %s, using DummyStatsLogger instead.", e)
                cls.__class__.instance = DummyStatsLogger()

    @classmethod
    def get_statsd_logger(cls):
        """Returns logger for statsd"""
        # no need to check for the scheduler/statsd_on -> this method is only called when it is set
        # and previously it would crash with None is callable if it was called without it.
        from statsd import StatsClient

        if conf.has_option('scheduler', 'statsd_custom_client_path'):
            stats_class = conf.getimport('scheduler', 'statsd_custom_client_path')

            if not issubclass(stats_class, StatsClient):
                raise AirflowConfigException(
                    "Your custom Statsd client must extend the statsd.StatsClient in order to ensure "
                    "backwards compatibility."
                )
            else:
                log.info("Successfully loaded custom Statsd client")

        else:
            stats_class = StatsClient

        statsd = stats_class(
            host=conf.get('scheduler', 'statsd_host'),
            port=conf.getint('scheduler', 'statsd_port'),
            prefix=conf.get('scheduler', 'statsd_prefix'))
        allow_list_validator = AllowListValidator(conf.get('scheduler', 'statsd_allow_list', fallback=None))
        return SafeStatsdLogger(statsd, allow_list_validator)

    @classmethod
    def get_dogstatsd_logger(cls):
        """Get DataDog statsd logger"""
        from datadog import DogStatsd
        dogstatsd = DogStatsd(
            host=conf.get('scheduler', 'statsd_host'),
            port=conf.getint('scheduler', 'statsd_port'),
            namespace=conf.get('scheduler', 'statsd_prefix'),
            constant_tags=cls.get_constant_tags())
        dogstatsd_allow_list = conf.get('scheduler', 'statsd_allow_list', fallback=None)
        allow_list_validator = AllowListValidator(dogstatsd_allow_list)
        return SafeDogStatsdLogger(dogstatsd, allow_list_validator)

    @classmethod
    def get_constant_tags(cls):
        """Get constanst DataDog tags to add to all stats"""
        tags = []
        tags_in_string = conf.get('scheduler', 'statsd_datadog_tags', fallback=None)
        if tags_in_string is None or tags_in_string == '':
            return tags
        else:
            for key_value in tags_in_string.split(','):
                tags.append(key_value)
            return tags


if TYPE_CHECKING:
    Stats: StatsLogger
else:
    class Stats(metaclass=_Stats):  # noqa: D101
        """Empty class for Stats - we use metaclass to inject the right one"""
