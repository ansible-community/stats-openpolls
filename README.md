# Open Ansible Community Polls

This repository builds a small Python web service for the [open community polls page](https://connect.eng.ansible.com/polls). It reads results from the Forum Data Explorer query, renders them at `/`, and provides the normalized records as JSON at `/data.json`.

The container is designed to run continuously behind an AWS EKS Service or ingress. It listens on port `8080`, runs as an unprivileged user, and provides `/healthz` and `/readyz` HTTP probes.

## Forum configuration

The service calls the open-polls Data Explorer query at:

`https://forum.ansible.com/admin/plugins/explorer/queries/2/run`

The API username defaults to `gundalow`. Supply the corresponding API key through the required `FORUM_POLL_API` environment variable. Do not commit the key to this repository or put it in `polls.yml`.

The query needs to return `topic_id`, `num`, `category_id`, `cat_name`, and `close_at`, plus a tag value. The service reads tag values from `tag_name` (the expected query column), or from `tag`, `tags`, `topic_tags`, or `tag_names`. If poll rows have no recognized tag column or contain no tag values, the service logs the returned columns and a warning to help diagnose an empty Tags column.

Set the key in your local shell before starting the service:

```sh
export FORUM_POLL_API="your-forum-api-key"
```

The service also accepts these optional, uppercase environment variables:

- `FORUM_POLL_USERNAME` — API username; defaults to `gundalow`
- `FORUM_POLL_URL` — Forum base URL; defaults to `https://forum.ansible.com`
- `FORUM_POLL_API_CALL` — Data Explorer path; defaults to `/admin/plugins/explorer/queries/2/run`
- `CONFIG_PATH` — YAML settings file; defaults to `/etc/open-polls/polls.yml`
- `CACHE_SECONDS` — successful or failed refresh interval; defaults to `300` (minimum `30`)

Environment variables take precedence over values in the YAML settings file. The included `polls.yml` contains only the non-secret defaults and is copied into the container at `/etc/open-polls/polls.yml`. Local runs use the same built-in defaults and do not need a settings file.

The service refreshes data when requested after the cache expires. If the Forum is unavailable, it serves the last successful snapshot and retries after the refresh interval. If no data has been loaded yet, `/data.json` returns HTTP 503. Expected configuration and Forum errors are logged as concise warnings without a traceback; unexpected errors are logged with a traceback and return a generic HTTP 503 message.

## HTTP endpoints

- `GET /` — responsive HTML page of open polls
- `GET /data.json` — JSON array with topic, category, closing date, tags, and links
- `GET /healthz` — process liveness probe; returns HTTP 200 while the server is running
- `GET /readyz` — readiness probe; returns HTTP 503 until poll data is available

Example JSON record:

```json
{
  "topic_id": 123,
  "post_number": 1,
  "category_id": 17,
  "category": "Community",
  "closes_at": "2026-12-31T00:00:00Z",
  "title": "A poll",
  "display_title": "A community poll",
  "slug": "a-community-poll",
  "tags": ["proposal"],
  "topic_url": "https://forum.ansible.com/t/123/1",
  "category_url": "https://forum.ansible.com/c/17"
}
```

## Run locally

Install [uv](https://docs.astral.sh/uv/) and run the service from this directory:

```sh
uv sync --locked
export FORUM_POLL_API="your-forum-api-key"
uv run gunicorn \
  --bind 0.0.0.0:8080 \
  --worker-class gthread \
  --workers 2 \
  --threads 4 \
  --timeout 60 \
  --access-logfile - \
  app:app
```

Gunicorn writes each HTTP request to the terminal because `--access-logfile -` sends access logs to standard output. Open <http://localhost:8080/> or request <http://localhost:8080/data.json>.

## Build and run the container

Build with Docker or Podman:

```sh
podman build --tag open-polls .
```

Run it with the Forum API key:

```sh
podman run --rm -p 8080:8080 \
  -e FORUM_POLL_API="$FORUM_POLL_API" \
  open-polls:latest
```

To override the non-secret YAML settings, mount a settings file at `/etc/open-polls/polls.yml`:

```sh
podman run --rm -p 8080:8080 \
  -e FORUM_POLL_API="$FORUM_POLL_API" \
  -v "$PWD/polls.yml:/etc/open-polls/polls.yml:ro" \
  open-polls:latest
```

## Deploy to AWS EKS

Publish a GitHub Release to trigger `.github/workflows/container.yml`. The workflow builds the `Dockerfile` and publishes the image to GitHub Container Registry. Release tags are published as image tags, and non-prerelease releases also update `latest`. The deployment image is:

`ghcr.io/ansible-community/stats-openpolls:latest`

GHCR packages are private by default. For unauthenticated EKS image pulls, change the package visibility to Public; [GitHub does not allow a public package to be made private again](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility). Otherwise, keep it private and configure an `imagePullSecret` with permission to read the package.

Create a Kubernetes Secret for the Forum API key:

```sh
kubectl create secret generic open-polls-discourse \
  --from-literal=api-key="$FORUM_POLL_API"
```

Reference that Secret from a Deployment and configure both HTTP probes on port `8080`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: open-polls
spec:
  replicas: 2
  selector:
    matchLabels:
      app: open-polls
  template:
    metadata:
      labels:
        app: open-polls
    spec:
      containers:
        - name: open-polls
          image: ghcr.io/ansible-community/stats-openpolls:latest
          ports:
            - name: http
              containerPort: 8080
          env:
            - name: FORUM_POLL_API
              valueFrom:
                secretKeyRef:
                  name: open-polls-discourse
                  key: api-key
          readinessProbe:
            httpGet:
              path: /readyz
              port: http
            periodSeconds: 30
            timeoutSeconds: 20
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            periodSeconds: 30
```

Expose the Deployment with a ClusterIP Service, then connect your chosen ingress or load balancer to the Service:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: open-polls
spec:
  type: ClusterIP
  selector:
    app: open-polls
  ports:
    - name: http
      port: 80
      targetPort: http
```

The container does not require a writable volume.

## Continuous integration and dependency updates

`.github/workflows/ci.yml` runs on pull requests and pushes to `main`. It checks Python formatting and lint with Ruff, checks types with Pyright, and audits GitHub Actions workflows with zizmor. Dependabot checks Python (`uv`), Docker base images, and GitHub Actions weekly, with a three day cooldown for version updates.
