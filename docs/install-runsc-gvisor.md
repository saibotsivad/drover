# Installing gVisor (`runsc`)

Part of the Drover architecture is to serve micro-containers with reasonably well isolated environments.

That means non-privileged micro-containers are launched using the `runsc` runtime ([gVisor](https://gvisor.dev/docs/)) which "provides a strong layer of isolation between running applications and the host operating system".

## Installing

You should install Docker in rootless mode, in which case installing the `runsc` runtime follows these steps:

1. **Install the latest release** - Visit the [gVisor installation instructions](https://gvisor.dev/docs/user_guide/install/)
   and run the shell script that downloads and moves the `runsc` binary to `/usr/local/bin`
2. **Register in Docker** - The `runsc install` installs to the global Docker config, so you need to register it
   manually in your rootless Docker config, which reads from `~/.config/docker/daemon.json`.
  a. First make sure `ls /usr/local/bin/runsc` shows the `runsc` binary.
  b. Then copy in the below `daemon.json` JSON into `~/.config/docker/daemon.json` (you may have to create the file).
  c. Restart Docker (rootless) with `systemctl --user restart docker`
  d. Then try running `hello-world` with the `runsc` runtime: `docker run --rm --runtime=runsc hello-world`

## `daemon.json`

```json
{
  "runtimes": {
    "runsc": {
      "path": "/usr/local/bin/runsc",
      "runtimeArgs": [
        "--ignore-cgroups"
      ]
    }
  }
}
```
