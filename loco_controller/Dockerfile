ARG BUILD_FROM
FROM $BUILD_FROM
RUN apk add --no-cache python3 py3-dbus py3-gobject3 py3-paho-mqtt \
    && (apk add --no-cache bluez-btmon || apk add --no-cache bluez || true)
COPY dualsense.py switchpro.py loco_controller.py /opt/loco-controller/
COPY run.sh /run.sh
RUN chmod 755 /run.sh
CMD ["/run.sh"]
