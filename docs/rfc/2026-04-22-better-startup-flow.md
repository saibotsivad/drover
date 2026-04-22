# goal

auto setup of repository

# short version

in @docs/rfc/2026-04-21-git-clone-startup.md we proposed a git clone at startup

the next step is to be able to specify an optional auto initialization for the repo

# general thought

we propose a file that lives at the repository root named `drover.yaml` that holds setup info

for example if your repo is a particular Nodejs version, or has some OS level dependencies (apt install imagemagik or whatever)

after the git clone step, we look for this `drover.yaml` file and run it to finish setup

we would need to defined some specification for the YAML
