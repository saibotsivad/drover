# goal

support git clone at startup, before `ready` signal

# short version

if you provide a git url as an env var it'll do a git clone before ready

# general thought

add a feature to the executor script:

- someone starting up a Drover mini-container would provide an environment variable named `DROVER_AUTO_GIT_URL` which would point to a git repository
- the normal executor script that starts with the mini-container would detect that environment variable, and if present it would do a git clone

# more thoughts

- it should support all classic git providers, and also Radicle (https://radicle.xyz)
- the git clone should happen to a consistent location independent of the git repo details, maybe something like `/opt/drover`
