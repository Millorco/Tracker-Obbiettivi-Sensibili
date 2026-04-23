#!/bin/bash
set -e

FTP_USER=${FTP_USER:-ftpuser}
FTP_PASS=${FTP_PASS:-ftppassword}

# Set FTP user password
echo "$FTP_USER:$FTP_PASS" | chpasswd

# Update vsftpd userlist
echo "$FTP_USER" > /etc/vsftpd.userlist

# Set PASV address if provided
if [ -n "$PASV_ADDRESS" ]; then
    sed -i "s/pasv_address=0.0.0.0/pasv_address=$PASV_ADDRESS/" /etc/vsftpd.conf
fi

# Ensure directories exist
mkdir -p /ftp-data/uploads /app/db /var/log/supervisor
chown -R $FTP_USER:$FTP_USER /ftp-data

exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
