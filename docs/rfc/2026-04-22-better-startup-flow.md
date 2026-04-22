proposed better startup flow

in docs/rfc/2026-04-21-git-clone-startup.md we proposed a git clone

the next step is to be able to include some optional auto initialization for the repo 

for example if your repo is a particular Nodejs version

so in this rfc we propose a root file like "drover.yaml" that holds setup info

the proposed change is that, after the git clone, we look for this file and run it to finish setup
