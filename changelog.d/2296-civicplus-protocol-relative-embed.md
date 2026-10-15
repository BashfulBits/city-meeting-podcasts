### Fixed

- **CivicPlus protocol-relative embed and HLS resolution.** Support protocol-relative URLs (`//...`)
  and normalize insecure or schemeless embed and stream URLs to HTTPS in `CivicPlusProvider`,
  resolving media extraction failures across CivicPlus cities (fixes #2296).
