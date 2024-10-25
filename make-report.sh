#!/usr/bin/bash

quarto render open_polls.qmd --output index.html && \
	rm -rf output/* && \
	mv index.html output/
