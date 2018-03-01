from ..firedex_configuration import FiredexConfiguration

import logging
log = logging.getLogger(__name__)


class FiredexAlgorithm(object):
    """
    Abstract base class for assigning topic priorities (i.e. topic-> net flow -> prio mappings) according to the current
    (estimated or theoretical) system configuration/state. Derived classes will share the same analytical model that
    uses our queuing network model, but will calculate the mappings differently.

    Note that these mappings are stored in this class as state and will only be updated for a given configuration if
    explicitly requested!
    """

    def __init__(self, **kwargs):
        # XXX: multiple inheritance
        try:
            super(FiredexAlgorithm, self).__init__(**kwargs)
        except TypeError:
            super(FiredexAlgorithm, self).__init__()

        # NOTE: we maintain a set of mappings and other state for each configuration being managed (i.e. broker and its
        # network ) AND for each subscriber connected with that broker.

        # these are filled in by _run_algorithm()
        self._topic_flow_map = dict()
        self._flow_prio_map = dict()

    ### Analytical model for queueing network
    ## NOTE: this model considers 4 queues:
    #  1) broker input queue for sorting/routing topics
    #  2) broker output queue for transmitting packets via network on different network flows
    #  3) SDN switch input queue for prioritization and dropping/bandwidth assignment by network flow
    #  4) SDN switch output queue (multi-class) for determining transmission rates of different topics according to the bandwidth

    def total_delays(self, configuration, subscriber=None):
        """
        Calculates the end-to-end delay of each topic on its route from the publisher(s) to the optionally-specified
        subscriber.  This includes queuing/service delays as well as network propagation delay (latency).

        :param configuration:
        :type configuration: FiredexConfiguration
        :param subscriber: the subscriber we calculate delays for
        :return: list of lists of service delays where each outer index corresponds to the
                topic sharing that index in config.topics
        :rtype: list[list[float]]
        """

        # TODO: consider pub-to-broker (averaged over pubs?) and broker-to-switch latency too?  maybe re-tx delay?
        return [configuration.latency + d for d in self.service_delays(configuration, subscriber=subscriber)]

    # ENHANCE: consider publisher-to-broker delay

    def service_delays(self, configuration, subscriber=None):
        """
        Calculates the service delay of each topic for each queue we model in our system.  Only considers the queues
        on route to the optionally-specified subscriber.

        :param configuration:
        :type configuration: FiredexConfiguration
        :param subscriber: the subscriber we calculate delays for
        :return: list of service delays where each index corresponds to the topic sharing that index in config.topics
        :rtype: list[float]
        """

        # We only really consider service rates for SDN switch outbound queue (i.e. due to bandwidth constraint).
        # ENHANCE: consider actual service rates for the other queues
        default_mu = 64000.0

        lambdas = self.delivery_rates(configuration, subscriber=subscriber, return_all_queues=True)

        # First the broker delays:
        # Each are MM1 queues
        lam_b_in = lambdas[0]
        mu_b_in = default_mu
        delta_b_in = [((1.0/mu_b_in)/(1-lam/mu_b_in)) for lam in lam_b_in]

        lam_b_out = lambdas[1]
        mu_b_out = default_mu
        delta_b_out = [((1.0/mu_b_out)/(1-lam/mu_b_out)) for lam in lam_b_out]

        # Then the SDN switch delays:
        lam_s_in = lambdas[2]
        mu_s_in = default_mu

        # First, the priority queue needs to consider each priority class according to the network flow/priority mappings.
        # So we get the total rates for each of these classes first.
        topic_prios = self.get_topic_priorities(configuration, subscriber=subscriber)
        lam_topics = zip(lam_s_in, configuration.topics)
        lam_prios = [sum(lam if topic_prios[top] == p else 0.0 for lam, top in lam_topics) for p in configuration.prio_classes]

        denom_all_prios = (mu_s_in - sum(lam_prios))
        delta_s_in = [(lam/((mu_s_in - sum(lam_prios[:topic_prios[top]])) * denom_all_prios)) for lam, top in lam_topics]

        # Now the multi-class queue where we consider a different mu per-topic
        lam_s_thru = lambdas[3]
        mu_s_thru = configuration.service_rates
        denom = (1.0 - sum(lam/mu for lam, mu in zip(lam_s_thru, mu_s_thru)))
        delta_s_out = [(1.0/mu)/denom for mu in mu_s_thru]

        final_delays = [delta_b_in, delta_b_out, delta_s_in, delta_s_out]
        final_delays = zip(*final_delays)
        final_delays = [sum(terms) for terms in final_delays]

        # Only topics for which the subscriber will actually receive events should have expected service delays!
        subs = set(configuration.subscriptions)
        final_lambdas = lambdas[5]
        final_delays = [d if (t in subs and l > 0.0) else 0.0 for t, d, l in zip(configuration.topics, final_delays, final_lambdas)]

        return final_delays

    def delivery_rates(self, configuration, subscriber=None, return_all_queues=False):
        """
        Returns the expected delivery rates of all topics for the optionally-specified subscriber.
        :param configuration:
        :param subscriber:
        :param return_all_queues: if set to True, returns a 6-tuple:
            (broker_in, broker_out, switch_in, switch_thru (i.e. arrival at multi-class queue), switch_out, subscriber_in)
        :return:
        """

        # ENHANCE: consider publisher queues?

        # First, consider the broker queues:
        lambdas_b_in = self.broker_arrival_rates(configuration)

        # we simply 0 out any topics for which no subscriptions
        # TODO: handle multiple subscribers!
        # NOTE: to do this, we'll have to potentially consider other subscribers anyway as a queue shared by two subs
        # still has arrival rates for topics to which one of those subscribers is not interested.
        subs = set(configuration.subscriptions)
        lambdas_b_out = [l if t in subs else 0.0 for t, l in zip(configuration.topics, lambdas_b_in)]
        # ENHANCE: per-flow lambdas?

        # Next, the SDN switch queues:
        # ENHANCE: consider drop rate en route to switch
        # TODO: consider our pre-emptive drop rate?
        lambdas_s_in = lambdas_b_out
        # ENHANCE: consider finite buffer size in prioq?
        lambdas_s_thru = lambdas_s_in
        lambdas_s_out = lambdas_s_thru

        # Lastly, the arrival rate at the subscriber consider packet errors
        lambdas_delivery = [(1 - configuration.error_rate) * l for l in lambdas_s_out]

        if return_all_queues:
            return lambdas_b_in, lambdas_b_out, lambdas_s_in, lambdas_s_thru, lambdas_s_out, lambdas_delivery
        else:
            return lambdas_delivery
    # alias this since queuing models typically refer to them as arrivals
    arrival_rates = delivery_rates

    def broker_arrival_rates(self, configuration):
        """
        Calculates the arrival rate of each topic at the broker by taking into account how many publishers advertise
        each topic i.e. arrival_rate[i] = pub_rate[i] * npubs_on_topic_i
        Note that no publishers on this topic results in a 0.0 arrival rate.
        :param configuration:
        :type configuration: FiredexConfiguration
        :return:
        :rtype: list[float]
        """

        # Start at 0 (no pubs) and fill in for each publishers' topics
        lambdas = {top: 0.0 for top in configuration.topics}
        for pub_class_ads in configuration.advertisements:
            for pub_ads in pub_class_ads:
                for topic in pub_ads:
                    lambdas[topic] += configuration.pub_rates[topic]
        lambdas = lambdas.items()
        lambdas.sort()
        lambdas = [v for (k,v) in lambdas]
        return lambdas

    def broker_departure_rates(self, configuration, arrival_rates, subscriber=None):
        """
        Calculates the departure rate of each topic from the broker to the specified subscirber by taking into account
        which topics it subscribes to.  Note that no subscriptions on a topic means a 0 departure rate.

        :param configuration:
        :type configuration: FiredexConfiguration
        :param arrival_rates: departure rates of topics from the broker
        :param subscriber:  currently ignored!!
        :return:
        :rtype: list[float]
        """

        # Start at 0 (no pubs) and fill in for each publishers' topics
        lambdas = {top: 0.0 for top in configuration.topics}
        for pub_class_ads in configuration.advertisements:
            for pub_ads in pub_class_ads:
                for topic in pub_ads:
                    lambdas[topic] += configuration.pub_rates[topic]
        lambdas = lambdas.items()
        lambdas.sort()
        lambdas = [v for (k,v) in lambdas]
        return lambdas

    def ros_okay(self, configuration):
        """
        Verifies if the "ro" condition is satisfied: whether the queues will have bounded sizes and not saturate over time.
        :param configuration:
        :return: True if condition satisfied, False otherwise
        """
        ros = self.get_ros(configuration)
        ros_okay = sum(ros) < 1.0
        return ros_okay

    def get_ros(self, configuration):
        """
        Verifies if the "ro" condition is satisfied: whether the queues will have bounded sizes and not saturate over time.
        :param configuration:
        :return:
        """

        ros = [lam / mu for lam, mu in zip(self.broker_arrival_rates(configuration), configuration.service_rates)]
        # log.info("ROs: %s\nRO total: %f" % (ros, sum(ros)))
        return ros

    ### Priority setting functions

    # TODO: consolidate_subscriber_flows=True as a param to the setters should ignore the specified subscriber and
    # set the flows/priorities for specified topics the same across all subscribers!  This might be used in a real
    # implementation in order to limit the number of OpenFlow flow rules used for prioritization

    def set_topic_net_flow(self, topic, net_flow, configuration, subscriber=None):
        """
        Set the network flow to be used for the given topic when forwarded to the specified subscriber.  Not specifying
        subscriber sets this flow for ALL subscribers in the given configuration.
        :param topic:
        :param net_flow:
        :param configuration:
        :type configuration: FiredexConfiguration
        :param subscriber: defaults to all subscribers
        :return:
        """

        if subscriber is None:
            subscribers = configuration.subscribers
        else:
            subscribers = [subscriber]

        for sub in subscribers:
            self._topic_flow_map.setdefault(configuration, dict()).setdefault(sub, dict())[topic] = net_flow

    def set_net_flow_priority(self, net_flow, priority, configuration, subscriber=None):
        """
        Set the priority class for the given network flow.
        :param net_flow:
        :param priority:
        :param configuration:
        :type configuration: FiredexConfiguration
        :param subscriber: defaults to all subscribers
        :return:
        """

        if subscriber is None:
            subscribers = configuration.subscribers
        else:
            subscribers = [subscriber]

        for sub in subscribers:
            self._flow_prio_map.setdefault(configuration, dict()).setdefault(sub, dict())[net_flow] = priority

    def get_topic_priorities(self, configuration, subscriber=None):
        """
        Runs the actual algorithm to determine what the priority levels should be according to the current real-time
        configuration specified.  This implementation just defers to the get_topic_net_flows() and
        get_net_flow_priorities() methods.

        :param configuration:
        :type configuration: FiredexConfiguration
        :param subscriber: which subscriber priorities being requested (default=arbitrarily pick one, which assumes
            the values were set across all subscribers)
        :return: mapping of topic IDs to priority classes
        :rtype: dict
        """

        if self._update_needed(configuration, subscriber):
            self._run_algorithm(configuration, subscriber)

        topic_flow_map = self.get_topic_net_flows(configuration, subscriber)
        flow_prio_map = self.get_net_flow_priorities(configuration, subscriber)

        topic_prio_map = {t: flow_prio_map[f] for t, f in topic_flow_map.items()}
        return topic_prio_map

    def get_topic_net_flows(self, configuration, subscriber=None):
        """
        Runs the algorithm to assign topics to network flows based on current configuration state.
        :param configuration:
        :type configuration: FiredexConfiguration
        :param subscriber: which subscriber net flows being requested (default=arbitrarily pick one, which assumes
            the values were set across all subscribers)
        :return: mapping of topic IDs to network flow IDs
        :rtype: dict
        """

        if self._update_needed(configuration, subscriber):
            self._run_algorithm(configuration, subscriber)
        if subscriber is None:
            # arbitrary subscriber from underlying mapping
            return next(self._topic_flow_map[configuration].itervalues())
        else:
            return self._topic_flow_map[configuration][subscriber]

    def get_net_flow_priorities(self, configuration, subscriber=None):
        """
        Runs the algorithm to assign network flows to priority levels based on current configuration state.
        :param configuration:
        :type configuration: FiredexConfiguration
        :param subscriber: which subscriber priorities being requested (default=arbitrarily pick one, which assumes
            the values were set across all subscribers)
        :return: mapping of network flow IDs to priority classes
        :rtype: dict
        """

        if self._update_needed(configuration, subscriber):
            self._run_algorithm(configuration, subscriber)
        if subscriber is None:
            # arbitrary subscriber from underlying mapping
            return next(self._flow_prio_map[configuration].itervalues())
        else:
            return self._flow_prio_map[configuration][subscriber]

    def force_update(self, configuration=None, subscribers=None):
        """
        Forces the algorithm to update the management (e.g. priorities) for certain configurations/subscribers.
        :param configuration: which configuration should be updated (default=all configs, which ignores subscribers arg)
        :type configuration: FiredexConfiguration
        :param subscribers: which subscribers for that configuration should be updated (default=all subscribers)
        """

        # to blow away all configurations, just reset everything
        if configuration is None:
            self._topic_flow_map = dict()
            self._flow_prio_map = dict()

        # for a specific configuration, just delete it IF IT EXISTS
        elif subscribers is None:
            self._topic_flow_map.pop(configuration, None)
            self._flow_prio_map.pop(configuration, None)

        # otherwise, carefully pop off each subscriber for the configuration (again, IF IT EXISTS)
        else:
            for subscriber in subscribers:
                self._topic_flow_map.get(configuration, dict()).pop(subscriber, None)
                self._flow_prio_map.get(configuration, dict()).pop(subscriber, None)

    ### Override these as necessary in derived algorithm classes

    def _run_algorithm(self, configuration, subscribers=None):
        """
        Runs the algorithm to assign network flows to priority levels based on current configuration state.
        :param configuration:
        :type configuration: FiredexConfiguration
        :param subscribers: which subscribers should have their priority levels assigned (default=None=all subscribers)
        """

        # NOTE: do this in derived class implementations to ensure you set the priorities for all subscribers!
        if subscribers is None:
            subscribers = configuration.subscribers

        raise NotImplementedError

    def _update_needed(self, configuration, subscriber=None):
        """
        Determines if an update is needed for the given configuration.  If the algorithm hasn't been run yet, this
        should return True.  This default base class implementation returns True if this config/sub has not been
        seen yet.  Hence, the calling class should precede this call with one to force_update(configuration) if the
        configuration has changed or enough time has passed! Base classes should probably override this method
        especially for actual system implementations to consider e.g. overhead of updating priorities.

        :param configuration:
        :type configuration: FiredexConfiguration
        :param subscriber: which subscriber may need updates (default=all subscribers)
        :return: True if the caller should do _run_algorithm(...) before proceeding
        :rtype: bool
        """

        # when we're checking all subscribers, need to make sure they're ALL present!
        if subscriber is None:
            subs = configuration.subscribers
        else:
            subs = [subscriber]

        update = configuration not in self._topic_flow_map or any(s not in self._topic_flow_map[configuration] for s in subs)
        return update