#!/usr/bin/env python
import rospy
from panda3d.core import BitMask32
from panda3d.bullet import BulletHelper
from panda3d.bullet import BulletRigidBodyNode
from car_srv import CarSrv

class Cory2SrvNode(CarSrv):

    def setup(self):
        # collision
        visNP = loader.loadModel('../models/coryf2.egg')
        visNP.clearModelNodes()
        visNP.reparentTo(render)
        pos = (7., 60.0, 3.8)
        visNP.setPos(pos[0], pos[1], pos[2])

        bodyNPs = BulletHelper.fromCollisionSolids(visNP, True);
        for bodyNP in bodyNPs:
            bodyNP.reparentTo(render)
            bodyNP.setPos(pos[0], pos[1], pos[2])

            if isinstance(bodyNP.node(), BulletRigidBodyNode):
                bodyNP.node().setMass(0.0)
                bodyNP.node().setKinematic(True)
                bodyNP.setCollideMask(BitMask32.allOn())
                self.world.attachRigidBody(bodyNP.node())

if __name__ == '__main__':
    rospy.init_node('car_node', anonymous=True)
    game = Cory2SrvNode()
    base.run()

