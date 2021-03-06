===============================
ovsdbapp
===============================

A library for creating OVSDB applications

The ovdsbapp library is useful for creating applications that communicate
via Open_vSwitch's OVSDB protocol (https://tools.ietf.org/html/rfc7047). It
wraps the Python 'ovs' and adds an event loop and friendly transactions.

* Free software: Apache license
* Documentation: http://docs.openstack.org/developer/ovsdbapp
* Source: http://git.openstack.org/cgit/openstack/ovsdbapp
* Bugs: http://bugs.launchpad.net/ovsdbapp

Features
--------

* An thread-based event loop for using ovs.db.Idl
* Transaction support
* Native OVSDB communication
