"""The configuration: a config directory with four YAML files.

``docs/features/configuration.md`` defines the schema, and
``docs/architecture.md`` the write paths and the reload semantics. This
package reads the directory, fills in environment variables, validates the
whole against the schema and the installed plugins, and writes the example
files of ``config init``.
"""

from bricklogger.config.examples import (
    EXAMPLES,
    FilesExist,
    examples_for,
    write_examples,
)
from bricklogger.config.instances import (
    InstanceNotFound,
    instances_in,
    remove_instance,
    set_instance,
)
from bricklogger.config.issues import ConfigError, ConfigIssue
from bricklogger.config.loader import (
    CONFIG_DIR_ENV,
    CONFIG_FILES,
    ENV_FILE,
    SYSTEM_CONFIG_DIR,
    SYSTEM_DATA_DIR,
    LoadResult,
    config_file,
    default_data_dir,
    load_configuration,
    parse_text,
    read_env_file,
    read_texts,
    resolve_config_dir,
    user_config_dir,
    user_data_dir,
    write_text,
)
from bricklogger.config.schema import (
    Configuration,
    DaemonSettings,
    DestinationInstance,
    NotificationSettings,
    Rule,
    Selector,
    SmtpSettings,
    SourceInstance,
)
from bricklogger.config.validation import (
    RESERVED_INSTANCE_NAMES,
    ValidationResult,
    not_installed,
    validate_configuration,
)

__all__ = [
    "CONFIG_DIR_ENV",
    "CONFIG_FILES",
    "ENV_FILE",
    "EXAMPLES",
    "RESERVED_INSTANCE_NAMES",
    "SYSTEM_CONFIG_DIR",
    "SYSTEM_DATA_DIR",
    "ConfigError",
    "ConfigIssue",
    "Configuration",
    "DaemonSettings",
    "DestinationInstance",
    "FilesExist",
    "InstanceNotFound",
    "LoadResult",
    "NotificationSettings",
    "Rule",
    "Selector",
    "SmtpSettings",
    "SourceInstance",
    "ValidationResult",
    "config_file",
    "default_data_dir",
    "examples_for",
    "instances_in",
    "load_configuration",
    "not_installed",
    "parse_text",
    "read_env_file",
    "read_texts",
    "remove_instance",
    "resolve_config_dir",
    "set_instance",
    "user_config_dir",
    "user_data_dir",
    "validate_configuration",
    "write_examples",
    "write_text",
]
