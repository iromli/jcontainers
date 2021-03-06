import contextlib
import io
import ipaddress
import json
import os
import pathlib
import re
import shutil
import socket
import time

import click
import click_spinner
import stdiomask
from compose.cli.command import get_project
from compose.cli.command import get_config_path_from_options
from compose.cli.main import TopLevelCommand
from compose.config.config import yaml
from compose.config.environment import Environment

from .settings import DEFAULT_SETTINGS
from .settings import COMPOSE_MAPPINGS

CONFIG_DIR = "volumes/config-init/db"
EMAIL_RGX = re.compile(
    r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
)
PASSWD_RGX = re.compile(
    r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*\W)[a-zA-Z0-9\S]{6,}$"
)


class ContainerHelper:
    def __init__(self, name, docker_client):
        self.name = name
        self.docker = docker_client

    def exec(self, cmd):  # noqa: A003
        exec_id = self.docker.exec_create(self.name, cmd).get("Id")
        retval = self.docker.exec_start(exec_id)
        retcode = self.docker.exec_inspect(exec_id).get("ExitCode")
        return retval, retcode


class Secret:
    UNSEAL_KEY_RE = re.compile(r"^Unseal Key 1: (.+)", re.M)
    ROOT_TOKEN_RE = re.compile(r"^Initial Root Token: (.+)", re.M)

    def __init__(self, docker_client):
        self.container = ContainerHelper("vault", docker_client)

    @contextlib.contextmanager
    def login(self):
        token = self.creds["token"]
        try:
            self.container.exec("vault login {}".format(token))
            yield
        except Exception:  # noqa: B902
            raise

    @property
    def creds(self):
        key = ""
        token = ""
        path = pathlib.Path("vault_key_token.txt")

        if path.is_file():
            txt = path.read_text()
            key = self.UNSEAL_KEY_RE.findall(txt)[0]
            token = self.ROOT_TOKEN_RE.findall(txt)[0]
        return {"key": key, "token": token}

    def status(self):
        print("[I] Checking Vault status")

        status = {}
        retry = 1
        while retry <= 3:
            raw, _ = self.container.exec("vault status -format yaml")
            with contextlib.suppress(yaml.scanner.ScannerError):
                status = yaml.safe_load(raw)
                if status:
                    break

            print("[W] Unable to get seal status in Vault; retrying ...")
            retry += 1
            time.sleep(5)
        return status

    def initialize(self):
        print("[I] Initializing Vault with 1 recovery key and token")
        out, _ = self.container.exec(
            "vault operator init "
            "-key-shares=1 "
            "-key-threshold=1 "
            "-recovery-shares=1 "
            "-recovery-threshold=1",
        )

        pathlib.Path("vault_key_token.txt").write_text(out.decode())
        print("[I] Vault recovery key and root token "
              "saved to vault_key_token.txt")

    def unseal(self):
        print("[I] Unsealing Vault manually")
        self.container.exec("vault operator unseal {}".format(self.creds["key"]))

    def write_policy(self, namespace="jans"):
        policies, _ = self.container.exec("vault policy list")
        if b"jans" in policies.splitlines():
            return

        print("[I] Creating Vault policy for Janssen")
        self.container.exec(f"vault policy write {namespace} /vault/config/policy.hcl")

    def enable_approle(self, namespace="jans"):
        raw, retcode = self.container.exec("vault auth list -format yaml")

        if retcode != 0:
            print(f"[E] Unable to get auth list; reason={raw.decode()}")
            raise click.Abort()

        auth_methods = yaml.safe_load(raw)

        if "approle/" in auth_methods:
            return

        print("[I] Enabling Vault AppRole auth")

        self.container.exec("vault auth enable approle")
        self.container.exec(f"vault write auth/approle/role/{namespace} policies={namespace}")
        self.container.exec(
            f"vault write auth/approle/role/{namespace}"
            "secret_id_ttl=0 "
            "token_num_uses=0 "
            "token_ttl=20m "
            "token_max_ttl=30m "
            "secret_id_num_uses=0"
        )

        role_id, _ = self.container.exec(f"vault read -field=role_id auth/approle/role/{namespace}/role-id")
        pathlib.Path("vault_role_id.txt").write_text(role_id.decode())

        secret_id, _ = self.container.exec(f"vault write -f -field=secret_id auth/approle/role/{namespace}/secret-id")
        pathlib.Path("vault_secret_id.txt").write_text(secret_id.decode())

    def setup(self, namespace="jans"):
        status = self.status()

        if not status["initialized"]:
            self.initialize()

        if status["sealed"]:
            time.sleep(5)
            self.unseal()

        time.sleep(5)
        with self.login():
            time.sleep(5)
            self.write_policy(namespace)
            time.sleep(5)
            self.enable_approle(namespace)


class Config(object):
    def __init__(self, docker_client):
        self.container = ContainerHelper("consul", docker_client)

    def hostname_from_backend(self, namespace="jans"):
        print("[I] Attempting to gather FQDN from Consul")

        hostname = ""
        retry = 1

        while retry <= 3:
            value, _ = self.container.exec(
                f"consul kv get -http-addr=http://consul:8500 {namespace}/config/hostname"
            )
            if not value.startswith(b"Error"):
                hostname = value.strip().decode()
                break

            print("[W] Unable to get FQDN from Consul; retrying ...")
            retry += 1
            time.sleep(5)
        return hostname

    def hostname_from_file(self, file_):
        hostname = ""
        with contextlib.suppress(FileNotFoundError, json.decoder.JSONDecodeError):
            data = json.loads(pathlib.Path(file_).read_text())
            hostname = data.get("_config", {}).get("hostname", "")
        return hostname


class App(object):
    def __init__(self):
        self.settings = self.get_settings()

    @contextlib.contextmanager
    def top_level_cmd(self):
        try:
            compose_files = self.get_compose_files()
            config_path = get_config_path_from_options(
                ".",
                {},
                {"COMPOSE_FILE": compose_files},
            )

            os.environ["COMPOSE_FILE"] = compose_files

            for k, v in self.settings.items():
                if k == "CONFIGURATION_OPTIONAL_SCOPES":
                    continue
                if isinstance(v, bool):
                    v = f"{v}".lower()
                os.environ[k] = v

            env = Environment()
            env.update(os.environ)

            project = get_project(os.getcwd(), config_path, environment=env)
            tlc = TopLevelCommand(project)
            yield tlc
        except Exception:  # noqa: B902
            raise

    def get_settings(self):
        """Get merged settings (default and custom settings from local Python file).
        """
        settings = DEFAULT_SETTINGS
        custom_settings = {}

        with contextlib.suppress(FileNotFoundError):
            path = pathlib.Path("settings.py")
            exec(compile(path.read_text(), path, "exec"), custom_settings)

        # make sure only uppercased settings are loaded
        custom_settings = {
            k: v for k, v in custom_settings.items()
            if k.isupper() and k in settings
        }

        settings.update(custom_settings)
        return settings

    def get_compose_files(self):
        files = ["docker-compose.yml"]
        for svc, filename in COMPOSE_MAPPINGS.items():
            if all([svc in self.settings, self.settings.get(svc), os.path.isfile(filename)]):
                files.append(filename)
        return ":".join(files)

    def logs(self, follow, tail, services=None):
        with self.top_level_cmd() as tlc:
            tlc.logs({
                "SERVICE": services or [],
                "--tail": tail,
                "--follow": follow,
                "--timestamps": False,
                "--no-color": False,
            })

    def config(self):
        with self.top_level_cmd() as tlc:
            tlc.config({
                "--resolve-image-digests": False,
                "--quiet": False,
                "--services": False,
                "--volumes": False,
                "--hash": None,
                "--no-interpolate": False,
            })

    def down(self):
        with self.top_level_cmd() as tlc:
            tlc.down({
                "--rmi": False,
                "--volumes": False,
                "--remove-orphans": True,
            })

    def _up(self, services=None):
        with self.top_level_cmd() as tlc:
            tlc.up({
                "SERVICE": services or [],
                "--no-deps": False,
                "--always-recreate-deps": False,
                "--abort-on-container-exit": False,
                "--remove-orphans": True,
                "--detach": True,
                "--no-recreate": False,
                "--force-recreate": False,
                "--build": False,
                "--no-build": True,
                "--scale": {},
                "--no-color": False,
                "--quiet-pull": False,
            })

    def ps(self, service):
        trap = io.StringIO()

        # suppress output of `ps` command
        with contextlib.redirect_stdout(trap):
            with self.top_level_cmd() as tlc:
                tlc.ps({
                    "--quiet": True,
                    "--services": False,
                    "--all": False,
                    "SERVICE": [service],
                })
        return trap.getvalue().strip()

    @property
    def network_name(self):
        with self.top_level_cmd() as tlc:
            return f"{tlc.project.name}_default"

    def gather_ip(self):
        """Gather IP address.
        """

        def auto_detect_ip():
            # detect IP address automatically (if possible)
            ip = ""
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                ip, _ = sock.getsockname()
            return ip

        print("[I] Attempting to gather external IP address")
        ip = self.settings["HOST_IP"] or auto_detect_ip() or click.prompt("Please input the host's external IP address")

        try:
            ipaddress.ip_address(ip)
            print(f"[I] Using {ip} as external IP address")
            self.settings["HOST_IP"] = ip
        except ValueError as exc:
            print(f"[E] Cannot determine IP address; reason={exc}")
            raise click.Abort()

    def generate_params(self, file_):
        def prompt_hostname():
            while True:
                value = click.prompt("Enter hostname", default="demoexample.jans.io")
                if len(value.split(".")) > 2:
                    return value
                click.echo("Hostname has to be at least three domain components.")

        def prompt_country_code():
            while True:
                value = click.prompt("Enter country code", default="US")
                if len(value) == 2 and value.isupper():
                    return value
                print("Country code must use 2 uppercased characters")

        def prompt_email():
            while True:
                value = click.prompt("Enter email", default="support@demoexample.jans.io")
                if EMAIL_RGX.match(value):
                    return value
                print("Invalid email address.")

        def prompt_password(prompt="Enter password: "):
            # FIXME: stdiomask doesn't handle CTRL+C
            while True:
                passwd = stdiomask.getpass(prompt=prompt)
                if not PASSWD_RGX.match(passwd):
                    print("Password must be at least 6 characters and include one uppercase letter, "
                          "one lowercase letter, one digit, and one special character.")
                    continue

                passwd_confirm = stdiomask.getpass(prompt="Repeat password: ")
                if passwd_confirm != passwd:
                    print("Both passwords are not equal")
                    continue
                return passwd

        def prompt_optional_scopes():
            allowed_scopes = ["ldap", "scim", "fido2", "redis", "couchbase"]
            allowed_scopes_fmt = ",".join(allowed_scopes)
            valid = True

            while True:
                value = click.prompt("Optional configuration scopes (space-separated value)", default="").split()

                for scope in value:
                    if scope not in allowed_scopes_fmt:
                        print(f"Unsupported {scope} scope; please choose one or more value from {allowed_scopes_fmt}")
                        valid = False
                        continue

                print(valid)
                if valid:
                    return value

        params = {}
        params["hostname"] = self.settings["DOMAIN"] or prompt_hostname()
        params["country_code"] = self.settings["COUNTRY_CODE"] or prompt_country_code()
        params["state"] = self.settings["STATE"] or click.prompt("Enter state", default="TX")
        params["city"] = self.settings["CITY"] or click.prompt("Enter city", default="Austin")
        params["admin_pw"] = self.settings["ADMIN_PW"] or prompt_password("Enter admin password: ")
        params["optional_scopes"] = self.settings["CONFIGURATION_OPTIONAL_SCOPES"] or prompt_optional_scopes()

        if "ldap" in params["optional_scopes"]:
            if self.settings["PERSISTENCE_TYPE"] in ("ldap", "hybrid"):
                params["ldap_pw"] = self.settings["LDAP_PW"] or prompt_password("Enter LDAP admin password: ")
            else:
                params["ldap_pw"] = params["admin_pw"]

        params["email"] = self.settings["EMAIL"] or prompt_email()
        params["org_name"] = self.settings["ORG_NAME"] or click.prompt("Enter organization", default="Janssen")

        if "redis" in params["optional_scopes"] and self.settings["CACHE_TYPE"] == "REDIS":
            params["redis_pw"] = self.settings["REDIS_PW"] or click.prompt("Enter Redis password: ", default="")

        pathlib.Path(file_).write_text(json.dumps(params, sort_keys=True, indent=4))
        return params

    def prepare_config_secret(self):
        workdir = os.getcwd()

        with self.top_level_cmd() as tlc:
            if not self.ps("consul"):
                self._up(["consul"])

            if not self.ps("vault"):
                self._up(["vault"])

            secret = Secret(tlc.project.client)
            secret.setup(self.settings["SECRET_NAMESPACE"])

            config = Config(tlc.project.client)

            hostname = config.hostname_from_backend(self.settings["CONFIG_NAMESPACE"])
            if hostname:
                self.settings["DOMAIN"] = hostname
                print(f"[I] Using {self.settings['DOMAIN']} as FQDN")
                return

            cfg_file = f"{workdir}/{CONFIG_DIR}/config.json"
            gen_file = f"{workdir}/generate.json"

            hostname = config.hostname_from_file(cfg_file)
            if hostname:
                self.settings["DOMAIN"] = hostname
            else:
                if not os.path.isfile(gen_file):
                    params = self.generate_params(gen_file)
                else:
                    with open(gen_file) as f:
                        params = json.loads(f.read())
                self.settings["DOMAIN"] = params["hostname"]

            print(f"[I] Using {self.settings['DOMAIN']} as FQDN")

    def up(self):
        self.check_ports()
        self.gather_ip()
        self.prepare_config_secret()
        self._up()
        self.healthcheck()

    def healthcheck(self):
        import requests
        import urllib3
        urllib3.disable_warnings()

        wait_max = 300
        wait_delay = 10

        print(
            "[I] Launching Janssen Server; to see logs on deployment process, "
            "please run 'logs -f' command on separate terminal"
        )
        with click_spinner.spinner():
            elapsed = 0
            while elapsed <= wait_max:
                with contextlib.suppress(requests.exceptions.ConnectionError):
                    req = requests.get(
                        f"https://{self.settings['HOST_IP']}/jans-auth/sys/health-check",
                        verify=False,
                    )
                    if req.ok:
                        print(f"\n[I] Janssen Server installed successfully; please visit https://{self.settings['DOMAIN']}")
                        return

                time.sleep(wait_delay)
                elapsed += wait_delay

            # healthcheck likely failed
            print(f"\n[W] Unable to get healthcheck status; please check the logs or visit https://{self.settings['DOMAIN']}")

    def touch_files(self):
        files = [
            "vault_role_id.txt",
            "vault_secret_id.txt",
            "gcp_kms_stanza.hcl",
            "gcp_kms_creds.json",
            "couchbase.crt",
            "couchbase_password",
            "couchbase_superuser_password",
            "jackrabbit_admin_password",
        ]
        for file_ in files:
            pathlib.Path(file_).touch()

    def copy_templates(self):
        entries = pathlib.Path(
            os.path.join(os.path.dirname(__file__), "templates")
        )
        curdir = os.getcwd()
        for entry in entries.iterdir():
            dst = os.path.join(curdir, entry.name)

            if os.path.exists(dst):
                print(f"[W] Skipping existing {dst}")
                continue

            shutil.copy(entry, dst)
            print(f"[I] Creating new {dst}")

    def check_ports(self):
        def _check(host, port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                conn = sock.connect_ex((host, port))
                if conn == 0:
                    # port is not available
                    return False
                return True

        with self.top_level_cmd() as tlc:
            ngx_run = tlc.project.client.containers(
                filters={"name": "nginx"}
            )
            if ngx_run:
                return

            # ports 80 and 443 must available if nginx has not run yet
            for port in [80, 443]:
                port_available = _check("0.0.0.0", port)
                if not port_available:
                    print(f"[W] Required port {port} is bind to another process")
                    raise click.Abort()

    def check_workdir(self):
        if not os.path.isfile("docker-compose.yml"):
            print("[E] docker-compose.yml file is not found; "
                  "make sure to run init command first")
            raise click.Abort()
