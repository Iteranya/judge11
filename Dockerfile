# Use a lightweight Ubuntu base
FROM ubuntu:24.04

# Install g++ and psmisc (psmisc gives us the 'killall' command we use for security cleanups)
RUN apt-get update && \
    apt-get install -y g++ psmisc && \
    rm -rf /var/lib/apt/lists/*

# Create a restricted user with UID 1000 so the --user "1000:1000" flag works perfectly
RUN useradd -u 1000 -M -s /bin/false sandbox

# Find <bits/stdc++.h> and Precompile it (PCH)
# We compile it with -std=c++17 to exactly match your run.sh script.
# This drops C++ compile times from ~800ms down to ~150ms.
RUN STDC_PATH=$(find /usr/include -name stdc++.h | grep bits | head -n 1) && \
    g++ -std=c++17 -O2 -x c++-header "$STDC_PATH" -o "$STDC_PATH.gch"