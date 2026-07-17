# Sandbox image

Build this image before running a BugAgent investigation. The sandbox refuses mutable image tags and uses `--pull=never` for every execution.

```powershell
docker build --tag bugagent-python-pytest:dev --file containers/python-pytest.Dockerfile .
docker image inspect bugagent-python-pytest:dev --format '{{.Id}}'
```

Use the returned `sha256:...` image ID as the `SandboxPolicy.image` value. Rebuild and record a new ID when the Dockerfile changes.
