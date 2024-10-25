FROM rocker/tidyverse:latest

RUN install2.r config httr gt \
&& rm -rf /tmp/downloaded_packages

RUN mkdir -p /opt/polls

WORKDIR /opt/polls
COPY ./open_polls.qmd .
COPY ./Ansible-Wordmark-Large-RGB-Black.png .
COPY ./make-report.sh .

CMD ["/bin/bash"]
