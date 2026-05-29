# Use a lightweight Ubuntu base
FROM ubuntu:24.04

# Install g++ and psmisc (psmisc gives us the 'killall' command)
RUN apt-get update && \
    apt-get install -y g++ psmisc && \
    rm -rf /var/lib/apt/lists/*

# Find <bits/stdc++.h> and Precompile it (PCH)
# We compile it with -std=c++17 to exactly match your run.sh script.
RUN STDC_PATH=$(find /usr/include -name stdc++.h | grep bits | head -n 1) && \
    g++ -std=c++17 -O2 -x c++-header "$STDC_PATH" -o "$STDC_PATH.gch"