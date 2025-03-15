FROM docker.elastic.co/kibana/kibana:5.6.16

RUN bin/kibana-plugin remove x-pack

RUN bash -c '\
    timeout 120 bash -c "/usr/local/bin/kibana-docker 2>&1" | \
    stdbuf -oL tee /dev/stderr | \
    grep -E -m 1 "Optimization of bundles for kibana, stateSessionStorageRedirect, timelion and status_page complete|Server running" && \
    echo "done"'
