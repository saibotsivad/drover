support git clone at startup

if you provide a git url as an env var it'll do a git clone before ready

proposed env var: DROVER_AUTO_GIT_URL

should support all classic git providers, and also Radicle (https://radicle.xyz)

the git clone should happen to a consistent location independent of the git repo details
