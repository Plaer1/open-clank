# Lore Protos

## Description

This crate contains the proto files and the build code to generate and expose a crate, using `prost` and `tonic`.
It also contains a package.json so the proto files can be published to artifactory.

## Publish
In order to publish do the following:

1. Go to the lore-proto folder:
```
cd lore-proto
```

2. Login to artifactory:
```
npm login --registry=<REGISTRY_URL> --auth-type=web
```
...and follow the instructions and login in the browser.

3. Version the package, changing the version number on `package.json` (e.g. 0.1.4). This should be in sync with the Cargo.toml version in this same folder.

4. Publish the package:
```
npm publish
```

5. Commit the updated package.json and Cargo.toml files to the repository.
