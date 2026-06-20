#!/home/david/.local/share/uv/tools/garmin-mcp/bin/python3
"""Create a running interval workout on Garmin Connect."""

import sys
sys.path.insert(0, '/home/david/.local/share/uv/tools/garmin-mcp/lib/python3.12/site-packages')

from garminconnect import Garmin
from garminconnect.workout import (
    RunningWorkout, WorkoutSegment, ExecutableStep, RepeatGroup,
    create_warmup_step, create_cooldown_step,
    ConditionType, TargetType, StepType,
)

TOKEN_DIR = '/home/david/.cache/garmin-mcp/garth/'

client = Garmin(email='', password='', return_on_mfa=False)
client.login(TOKEN_DIR)

# Pace target: 3:35-3:40/km → 4.545–4.651 m/s
pace_target = {
    "workoutTargetTypeId": TargetType.PACE_ZONE,
    "workoutTargetTypeKey": "pace.zone",
    "displayOrder": 6,
    "targetValueOne": 4.545,   # 3:40/km (slower bound)
    "targetValueTwo": 4.651,   # 3:35/km (faster bound)
}

no_target = {
    "workoutTargetTypeId": TargetType.NO_TARGET,
    "workoutTargetTypeKey": "no.target",
    "displayOrder": 1,
}

# 1km interval
interval = ExecutableStep(
    stepOrder=1,
    childStepId=1,
    stepType={"stepTypeId": StepType.INTERVAL, "stepTypeKey": "interval", "displayOrder": 3},
    endCondition={"conditionTypeId": ConditionType.DISTANCE, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True},
    endConditionValue=1000.0,
    targetType=pace_target,
    targetValueOne=4.545,   # 3:40/km (slower bound)
    targetValueTwo=4.651,   # 3:35/km (faster bound)
)

# 90s recovery jog
recovery = ExecutableStep(
    stepOrder=2,
    childStepId=2,
    stepType={"stepTypeId": StepType.RECOVERY, "stepTypeKey": "recovery", "displayOrder": 4},
    endCondition={"conditionTypeId": ConditionType.TIME, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
    endConditionValue=90.0,
    targetType=no_target,
)

# 5 repetitions
repeat = RepeatGroup(
    stepOrder=2,
    stepType={"stepTypeId": StepType.REPEAT, "stepTypeKey": "repeat", "displayOrder": 6},
    numberOfIterations=5,
    workoutSteps=[interval, recovery],
    endCondition={"conditionTypeId": ConditionType.ITERATIONS, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
    endConditionValue=5.0,
)

workout = RunningWorkout(
    workoutName="Series 5x1km @ 3:35-3:40/km",
    description="Calentamiento 10min + 5x1km a 3:35-3:40/km con 90s recuperacion + vuelta calma 10min",
    estimatedDurationInSecs=3300,
    workoutSegments=[
        WorkoutSegment(
            segmentOrder=1,
            sportType={"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
            workoutSteps=[
                create_warmup_step(600.0, step_order=1),
                repeat,
                create_cooldown_step(600.0, step_order=3),
            ],
        )
    ],
)

import json
result = client.upload_running_workout(workout)
print(json.dumps(result, indent=2))
