#!/usr/bin/env python

# Imports
import dbus
from dbus.mainloop.glib import DBusGMainLoop
import glib
import time
import os
import sys
import datetime
import logging
import copy
from logging.handlers import RotatingFileHandler
from settings import settingsdict, servicesdict, pvdict, donotcalclist # Change this for production
from ext.velib_python.vedbus import VeDbusItemImport


class SystemController(object):


    def __init__(self, bus):

        self.bus = bus
        self.settings = copy.deepcopy(settingsdict)
        self.dbusservices = copy.deepcopy(servicesdict)
        self.pvservices = copy.deepcopy(pvdict)
        self.donotcalc = copy.deepcopy(donotcalclist)

        self.safetylistcounter = 0
        self.outputpowerlist = [0 for i in range(0, self.settings['Safety']['BuildupIterations'])]
        self.prevruntime = datetime.datetime.now()
        self.unavailableservices = []
        self.unavailablepvinverters = []
        self.powerlimit = 0
        self.throttleactive = False
        self.insurplus = 0
        self.pvcontrollable = True # TODO make this a true check later
        self.rescan_service_time = datetime.datetime.now()

        # Ensure this is always at the bottom
        self.setup_dbus_services()

    def setup_dbus_services(self):

        for service in self.dbusservices:
            try:
                self.dbusservices[service]['Proxy'] = VeDbusItemImport(
                    bus=self.bus,
                    serviceName=self.dbusservices[service]['Service'],
                    path=self.dbusservices[service]['Path'],
                    eventCallback=self.update_values,
                    createsignal=True)
            except:
                mainlogger.error('Exception in setting up dbus service %s' % service)
                self.unavailableservices.append(service)

        # Also set up the pv inverter services
        for line in self.pvservices:
            for inverter, invservices in self.pvservices[line]['Inverters'].items():
                try:
                    for service in invservices:
                        invservices[service]['Proxy'] = VeDbusItemImport(
                            bus=self.bus,
                            serviceName=invservices[service]['Service'],
                            path=invservices[service]['Path'],
                            eventCallback=self.update_values,
                            createsignal=True)
                except:
                    mainlogger.error('Exception in setting up pv inverter %s' % inverter)
                    self.unavailablepvinverters.append(inverter)

    def update_values(self, name, path, changes):

        # Update the dbusservices dictionary
        for service in self.dbusservices:
            if service not in self.unavailableservices:
                try:
                    self.dbusservices[service]['Value'] = self.dbusservices[service]['Proxy'].get_value()
                except dbus.DBusException:
                    mainlogger.warning('Exception in getting dbus service %s' % service)
                try:
                    self.dbusservices[service]['Value'] *= 1
                except:
                    mainlogger.warning('Non numeric value on %s' % service)
                    # Use the default value as in settings.py
                    self.dbusservices[service]['Value'] = servicesdict[service]['Value']
        # Update the pvservices dictionary
        for line in self.pvservices:
            for inverter, invservices in self.pvservices[line]['Inverters'].items():
                if inverter not in self.unavailablepvinverters:
                    for service in invservices:
                        try:
                            invservices[service]['Value'] = invservices[service]['Proxy'].get_value()
                        except dbus.DBusException:
                            mainlogger.warning('Exception in getting dbus service %s for %s' % (service, inverter))
                        try:
                            invservices[service]['Value'] *= 1
                        except:
                            mainlogger.warning('Non numeric value on %s' % service)
                            # Use the default value as in settings.py
                            invservices[service]['Value'] = pvdict[line]['Inverters'][inverter][service]['Value']

        # Do not do calculations on this list
        if path not in self.donotcalc:
            self.do_calcs()

    def set_value(self, service, value, dictionary = None):
        # TODO this is a temporary fix, remove the default value later
        if dictionary is None:
            dictionary = self.dbusservices

        if service not in self.unavailableservices:
            try:
                VeDbusItemImport(
                    bus=self.bus,
                    serviceName=dictionary[service]['Service'],
                    path=dictionary[service]['Path'],
                    eventCallback=None,
                    createsignal=False).set_value(value)
            except dbus.DBusException:
                mainlogger.warning('Exception in setting dbus service %s' % service)

    # Charge the batteries to allow the cell voltages to equalize
    def charge(self):

        if self.settings['DoCharge'] is True:
            if self.settings['ChargeActive'] is False:
                if datetime.date.today() >= self.settings['ChargeDate']:
                    if datetime.datetime.now().time() >= self.settings['ChargeStartTime']:
                        if datetime.datetime.now().weekday() == self.settings['ChargeDay']:
                            self.settings['ChargeActive'] = True
                            self.settings['ChargeEndTime'] = datetime.datetime.now() + self.settings['ChargeDuration']
                            self.settings['ChargeDate'] += self.settings['ChargeInterval']
                        else:
                            self.settings['ChargeDate'] += datetime.timedelta(days=1)
            else:
                if datetime.datetime.now() >= self.settings['ChargeEndTime']:
                    self.settings['ChargeActive'] = False

    def control_pv(self, soc):

        # Update the pv inverter combined list
        solartotals = {}
        for line in self.pvservices:
            solartotals[line] = {'Power': 0, 'MaxPower': 0}
            for inverter, invservices in self.pvservices[line]['Inverters'].items():
                if inverter not in self.unavailablepvinverters:
                    solartotals[line]['Power'] += invservices['Power']['Value']
                    solartotals[line]['MaxPower'] += invservices['MaxPower']['Value']
            mainlogger.debug('PV total power: %s' % solartotals[line]['Power'])
            mainlogger.debug('PV Max available power: %s' % solartotals[line]['MaxPower'])

        # Control the fronius inverter to prevent feed in
        if self.dbusservices['L1InPower']['Value'] < self.settings['MinInPower'] - self.settings['ThrottleBuffer']:
            self.powerlimit = solartotals['L1']['Power'] \
                              - (self.settings['MinInPower']
                                 - self.dbusservices['L1InPower']['Value']
                                 + self.settings['OverThrottle'])
            self.throttleactive = True
            self.insurplus = self.settings['MinInPower'] \
                             + self.settings['OverThrottle'] \
                             - self.dbusservices['L1InPower']['Value']
            mainlogger.debug('Starting to throttle')
        # Increase the powerlimit so that we can utilize the solar power
        elif self.throttleactive:
            self.powerlimit = self.powerlimit + self.settings['ThrottleBuffer']
            self.insurplus = max(self.insurplus - self.settings['ThrottleBuffer'], 0)
            mainlogger.debug('Increasing PV power slowly and reducing insurplus')
            if solartotals['L1']['Power'] < self.powerlimit + (2 * self.settings['ThrottleBuffer']):
                self.throttleactive = False
                self.insurplus = 0
                mainlogger.debug('Throttling no longer required')
        # Keep limiting the inverter to a value slightly higher than the current power to prevent spikes in solar power
        # even when there is no need for actual throttling
        if not self.throttleactive:
            self.powerlimit = solartotals['L1']['Power'] + self.settings['ThrottleBuffer']
        # Strongly throttle the inverter once the strongthrottle SOC has been reached
        if soc >= self.settings['StrongThrottleMinSoc']:
            strongthrottlevalue = (soc - self.settings['StrongThrottleMinSoc']) \
                                  * self.settings['StrongThrottleBuffer']\
                                  / (self.settings['StrongThrottleMaxSoc'] - self.settings['StrongThrottleMinSoc'])
            self.powerlimit = self.dbusservices['L1OutPower']['Value'] - strongthrottlevalue
            mainlogger.debug('Strong throttling active')
        # Prevent the powerlimit from being larger than the max inverter power or negative
        if self.powerlimit > solartotals['L1']['MaxPower']:
            self.powerlimit = solartotals['L1']['MaxPower']
        elif self.powerlimit < 0:
            self.powerlimit = 0
        mainlogger.debug('PV Powerlimit: %s' % self.powerlimit)

        # set the fronius powerlimit for each inverter in proportion to the total power currently being produced
        # Ensure inv_count is always at least 1
        inv_count = max(len(self.pvservices['L1']['Inverters']), 1)
        for inverter, invservices in self.pvservices['L1']['Inverters'].items():
            if inverter not in self.unavailablepvinverters:
                if solartotals['L1']['Power'] == 0:
                    inverterpowerlimit = self.settings['ThrottleBuffer'] / inv_count
                # Ensure that the throttle buffer gets distributed evenly between the inverters
                elif not self.throttleactive:
                    inverterpowerlimit = invservices['Power']['Value'] + \
                                         self.settings['ThrottleBuffer'] / inv_count
                else:
                    inverterpowerlimit = self.powerlimit * (invservices['Power']['Value'] / solartotals['L1']['Power'])
                self.set_value('PowerLimit', inverterpowerlimit, invservices)
                mainlogger.debug('Setting inverter %s powerlimit to %s' % (inverter, inverterpowerlimit))

    def run(self):

        # Do calcs manually
        delta = datetime.datetime.now() - self.prevruntime
        if delta >= datetime.timedelta(seconds=self.settings['MaxSleepTime'] - self.settings['LoopCheckTime']):
            self.do_calcs()
            mainlogger.warning('Manually running do_calcs')
        # Let this function run continually on the glib loop
        return True


    def do_calcs(self):

        # Setup variables
        weekend = False
        minin = self.settings['MinInPower']
        soc = self.dbusservices['Soc']['Value']
        outpower = max(self.dbusservices['L1OutPower']['Value'], 0)

        mainlogger.debug('SOC: %s' % soc)
        mainlogger.debug('outpower: %s' % outpower)

        # Update the runtime variable
        self.prevruntime = datetime.datetime.now()

        # Set the stable battery SOC depending on the weekday
        # The weekend ends in the same week as it starts (eg. sunday)
        if self.settings['WeekendEndDay'] >= self.settings['WeekendStartDay']:
            # Weekend has started
            if datetime.datetime.now().weekday() >= self.settings['WeekendStartDay'] and \
                    datetime.datetime.now().time() >= self.settings['WeekendStartTime']:
                weekend = True
            # Weekend has ended
            if datetime.datetime.now().weekday() >= self.settings['WeekendEndDay'] and \
                        datetime.datetime.now().time() >= self.settings['WeekendEndTime']:
                weekend = False
        # The weekend ends during the week after the weekend started
        else:
            # Week has started
            if datetime.datetime.now().weekday() >= self.settings['WeekendEndDay'] and \
                        datetime.datetime.now().time() >= self.settings['WeekendEndTime']:
                weekend = False
            # Week has ended
            if datetime.datetime.now().weekday() >= self.settings['WeekendStartDay'] and \
                    datetime.datetime.now().time() >= self.settings['WeekendStartTime']:
                weekend = True
        if weekend:
            stablebatterysoc = self.settings['WeekendStableBatterySoc']
        else:
            stablebatterysoc = self.settings['WeekStableBatterySoc']
        mainlogger.debug('stablebatterysoc: %s' % stablebatterysoc)

        # Set the correct inputpower
        # The powerslope is used to calculate the inputpower, 0.2 corresponds to 20%
        powerslope = (1 - 0.2) / (self.settings['20%PowerSoc'] - stablebatterysoc)
        # Battery is lower than the setpoint, set inpower = recharge power + outpower
        if soc <= stablebatterysoc - 1:
            inpower = (2.0 * (stablebatterysoc - soc) / 100) \
                      * (self.settings['BatteryCapacity'] /self.settings['LowBatteryRechargeTime']) \
                      + outpower
            mainlogger.debug('Battery soc lower than setpoint')
        # Battery is above the 20% power value, set inpower = 20% of outpower + constant inpower
        elif soc >= self.settings['20%PowerSoc']:
            inpower = 0.2 * outpower
            mainlogger.debug('Battery higher than 20% power value')
        # Battery is in the powerslope area, use it to calculate the inpower
        else:
            inpower = outpower * (1 - (soc - stablebatterysoc) * powerslope)
            mainlogger.debug('Battery in powerslope')

        # Do a charge if the conditions are met
        self.charge()
        if self.settings['ChargeActive']:
            inpower = outpower + self.settings['ChargePower']
            mainlogger.debug('Charging active')

        # Safety mechanism to prevent low input power during high power use
        # Mark the data points where outpower is higher than the safety value
        maxoutpower = outpower
        if maxoutpower >= self.settings['Safety']['BuildupThreshold']:
            self.outputpowerlist[self.safetylistcounter] = 1
        else:
            self.outputpowerlist[self.safetylistcounter] = 0
        # Set the correct counter value for the next iteration
        if self.safetylistcounter < self.settings['Safety']['BuildupIterations'] - 1:
            self.safetylistcounter += 1
        else:
            self.safetylistcounter = 0
        # Check if the maxoutpower has exceeded the critical threshold
        if maxoutpower > self.settings['Safety']['CriticalThreshold']:
            minin = maxoutpower - self.settings['Safety']['MaxInverterPower']
            self.settings['Safety']['EndTime'] = datetime.datetime.now() + self.settings['Safety']['Duration']
            self.settings['Safety']['Active'] = True
            mainlogger.debug('Maxoutput has exceeded the critical threshold')
        # Check if the maxoutpower has surpassed the buildup threshold
        elif sum(self.outputpowerlist) >= self.settings['Safety']['BuildupIterations'] *\
                self.settings['Safety']['BuildupPercentage'] / 100:
            minin = maxoutpower - self.settings['Safety']['MaxInverterPower']
            self.settings['Safety']['EndTime'] = datetime.datetime.now() + self.settings['Safety']['Duration']
            self.settings['Safety']['Active'] = True
            mainlogger.debug('Maxoutput has exceeded the buildup threshold')
        # Check if the safety period has ended
        else:
            if self.settings['Safety']['EndTime'] < datetime.datetime.now():
                minin = self.settings['MinInPower']
                self.settings['Safety']['Active'] = False
                mainlogger.debug('Safety is no longer active')

        # Check if the pv can be controlled and if so, then do it
        # Only control the pv if connected to mains
        if self.pvcontrollable and self.dbusservices['InputSource']['Value'] != 240:
            self.control_pv(soc)
        else:
            self.insurplus = 0
            mainlogger.debug('PV is not controllable. Setting to control pv is set to %s and the input source is %s'
                             % (self.pvcontrollable, self.dbusservices['InputSource']['Value']))


        inpower = max(minin, inpower) + self.insurplus

        # Send the inputpower to the CCGX control loop
        self.set_value('AcSetpoint', inpower)

        # Log debug info
        mainlogger.debug('inpower: %s' % inpower)
        mainlogger.debug('mininpower: %s' % minin)
        mainlogger.debug('End of list')

        # Rescan the services if the correct amount of time has elapsed
        if datetime.datetime.now() >= self.rescan_service_time:
            self.unavailableservices = []
            self.unavailablepvinverters = []
            self.setup_dbus_services()
            self.rescan_service_time = datetime.datetime.now() + self.settings['RescanServiceInterval']


if __name__ == "__main__":

    # Create a rotating logger
    def create_rotating_log(path):
        # Create the logger
        logger = logging.getLogger("Main Log")
        logger.setLevel(logging.INFO)
        # Create a rotating handler
        handler = RotatingFileHandler(path, maxBytes=1048576, backupCount=5)
        # Create a formatter and add to handler
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        # Add the handler to the logger
        logger.addHandler(handler)
        return logger

    # setup the logger
    log_file = "log.txt"
    mainlogger = create_rotating_log(log_file)
    # Setup the dbus
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    # start the controller
    mainlogger.debug('Starting SystemController')
    controller = SystemController(bus)
    glib.timeout_add_seconds(controller.settings['LoopCheckTime'], controller.run)
    mainloop = glib.MainLoop()
    mainloop.run()