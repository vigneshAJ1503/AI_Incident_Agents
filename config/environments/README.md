# Moved: environments are now company profiles

`config/environments/<name>.yaml` and `config/service-catalog/<name>.yaml` became
`profiles/<name>/profile.yaml` and `profiles/<name>/services.yaml` (PR-P1, ADR-0011,
[docs/portability.md](../../docs/portability.md)).

Back-compat: a `<name>.yaml` placed here still loads (with its catalog in
`config/service-catalog/`) when no `profiles/<name>/` exists, and `AIOPS_ENV` is a
deprecated alias of `AIOPS_PROFILE`. To migrate:

```bash
mkdir profiles/<name>
mv config/environments/<name>.yaml     profiles/<name>/profile.yaml
mv config/service-catalog/<name>.yaml  profiles/<name>/services.yaml
# then drop `environment:` and `service_catalog:` (the folder name is the profile name)
```
