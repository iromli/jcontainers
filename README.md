![pygluu compose](https://github.com/GluuFederation/community-edition-containers/workflows/pygluu%20compose/badge.svg?branch=4.2)
# Gluu Server Community Edition Containers

[Gluu Server Community Edition Documentation](https://gluu.org/docs/gluu-server/4.2/)

## Prerequisites

1.  Python 3.6+.
1.  Python `pip3` package.

## Quickstart

1.  Install packages (depends on OS):

    - `curl`
    - `wget`
    - `python3` (Python 3.6 and above)
    - `python3-distutils` (only for Ubuntu/Debian)

1.  Install [docker](https://docs.docker.com/install/)

1.  Get `pygluu-compose` executable:

    1.  Install [shiv](https://shiv.readthedocs.io/) using `pip3`:

        ```sh
        pip3 install shiv
        ```

    1.  Install the package:

        ```sh
        make zipapp
        ```

        This command will generate executable called `pygluu-compose.pyz` under current directory.

1.  Run the following commands to start deploying containers:

    ```sh
    mkdir -p examples/single-host
    cd examples/single-host
    pygluu-compose.pyz init
    pygluu-compose.pyz up
    ```

    Or check available commands by running:

    ```sh
    pygluu-compose.pyz -h
    ```

## Issues

If you find any issues, please post them on the customer support portal, [support.gluu.org](https://support.gluu.org).
