ARG BUILD_FROM
FROM $BUILD_FROM
RUN apk add --no-cache python3 py3-dbus py3-gobject3 tzdata
COPY loco_orologio.py /opt/loco-orologio/loco_orologio.py
COPY run.sh /run.sh
RUN chmod 755 /run.sh
CMD ["/run.sh"]
