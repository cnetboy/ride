import random

from firedex_algorithm import FiredexAlgorithm
from ..firedex_configuration import FiredexConfiguration

import logging
log = logging.getLogger(__name__)


class RandomFiredexAlgorithm(FiredexAlgorithm):
    """
    Algorithm implementation that assigns priorities randomly (uniform dist. across all priority classes) for a
    baseline comparison with actual algorithms.
    """

    def __init__(self, seed=None, **kwargs):
        super(RandomFiredexAlgorithm, self).__init__(**kwargs)
        self.rand = random.Random(seed)

    def _run_algorithm(self, configuration, subscribers=None):
        """
        :param configuration:
        :type configuration: FiredexConfiguration
        :param subscribers:
        :return:
        """
        if subscribers is None:
            subscribers = configuration.subscribers

        for sub in subscribers:
            flows = configuration.net_flows_for_subscriber(sub)
            for req in configuration.get_subscriptions(sub):
                flow = self.rand.sample(flows, 1)[0]
                self.set_subscription_net_flow(req, flow, configuration)

            # XXX: because we currently assume nflows == nprios, we need to make sure each prioq is used here or we end up
            # with e.g. only 2/3 priorities used!  Hence, for now we just assign flows to directly map to their
            # corresponding priority: in the future we should start by sampling priorities for net flows to ensure each
            # prio is used, and then re-sample until all remaining net flows are used up.

            prios = configuration.prio_classes
            if len(flows) != len(prios):
                raise NotImplementedError("We currently assume that #flows == #prio classes always!  No support yet for"
                                          "randomly assigning mappings otherwise... Requested %d flows and %d priorities" %\
                                          (configuration.num_net_flows, configuration.num_priority_levels))

            for f, p in zip(flows, prios):
                self.set_net_flow_priority(f, p, configuration)