## Docker Container for Open Forum Polls

This container handles the generation of the [Open Polls page](https://connect.eng.ansible.com/polls).

## Setup

This container requires three mount points:
- a `config` dir mounted to `/srv/docker-config/polls` for the tokens, lists, and email config
- a `site` dir mounted to `/opt/polls/output` for the rendered site output

### Example dir layout

Inside the container it should look like this:
```
/srv/docker-config
└── polls
    └── polls.yml
/srv/docker-reports
└── polls
```

## Config

You can look at the examples in this repo for details. You'll just need a
`polls.yml` to configure the connection to Discourse. The `api_call` variable
points to the path in the Data Explorer that actually gets the polls from the
Discourse database, you'll need to configure that too (in Discourse).

## Build the container

```
podman build --tag openpolls .
```

## Run the container

### Default

The default action will give you a shell:

```
podman run --rm -ti \
  -v /srv/docker-config/polls:/srv/docker-config/polls \
  -v /srv/docker-reports/polls:/opt/polls/output \
  openpolls:latest 
```

### Generate report site

The site is built using [Quarto](https://quarto.org):

```
podman run --rm -ti \
  -v /srv/docker-config/polls:/srv/docker-config/polls \
  -v /srv/docker-reports/polls:/opt/polls/output \
  openpolls:latest \
  ./make-report.sh
```

This will output the rendered page to the mounted `output` directory.
