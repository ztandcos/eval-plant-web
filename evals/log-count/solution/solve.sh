#!/bin/sh
grep -c ERROR /var/log/app/app.log > /app/count.txt
